"""业务流程编排"""
import time
import json
from datetime import datetime
from typing import Any
from .clients.dajian_client import DaJianClient
from .clients.ebay_client import EbayClient
from .clients.ebay_trading_client import EbayTradingClient
from .services.gemini_optimizer import GeminiOptimizer
from .db.database import Database
import html


class ListingPipeline:
    """产品刊登流程"""
    
    def __init__(
        self,
        dajian_client: DaJianClient,
        ebay_client: EbayClient,
        gemini_optimizer: GeminiOptimizer,
        db: Database
    ):
        """
        初始化 Pipeline
        
        Args:
            dajian_client: 大建云仓客户端
            ebay_client: eBay 客户端
            gemini_optimizer: Gemini 优化器
            db: 数据库实例
        """
        self.dajian = dajian_client
        self.ebay = ebay_client
        self.gemini = gemini_optimizer
        self.dajian = dajian_client
        self.ebay = ebay_client
        self.trading = EbayTradingClient(ebay_client)
        self.gemini = gemini_optimizer
        self.db = db
    
    def run_full_sync(self, batch_size: int = 50):
        """
        完整同步流程
        
        步骤：
        1. 从大建云仓拉取所有产品
        2. 存入数据库（去重）
        3. 批量 AI 优化
        4. 逐个刊登到 eBay
        5. 更新 ItemID 和状态
        
        Args:
            batch_size: 每批处理数量
        """
        print("=" * 60)
        print("开始完整同步流程")
        print("=" * 60)
        
        # 步骤 1：拉取产品列表
        print("\n[1/4] 从大建云仓拉取产品...")
        page = 1
        total_fetched = 0
        
        while True:
            try:
                products = self.dajian.get_product_list(page=page, page_size=batch_size)
                if not products:
                    break
                
                print(f"  拉取第 {page} 页，共 {len(products)} 个产品")
                
                # 存入数据库
                for product in products:
                    sku = product.get('sku')
                    if not sku:
                        continue
                    
                    # 获取产品详情
                    detail = self.dajian.get_product_detail(sku)
                    
                    self.db.save_product({
                        'sku': sku,
                        'dajian_title': detail.get('title', ''),
                        'dajian_description': detail.get('description', ''),
                        'dajian_price': detail.get('price', 0),
                        'dajian_stock': detail.get('stock', 0),
                        'dajian_category': product.get('category', ''),
                        'images': detail.get('images', []),
                        'sync_status': 'pending'
                    })
                    
                    total_fetched += 1
                    self.db.log_sync(sku, 'fetch', 'success', '产品已拉取')
                
                page += 1
                time.sleep(0.5)  # 避免 Rate Limit
                
            except Exception as e:
                print(f"  拉取失败: {str(e)}")
                break
        
        print(f"  ✓ 共拉取 {total_fetched} 个产品\n")
        
        # 步骤 2：批量 AI 优化
        print("[2/4] 批量 AI 优化...")
        pending = self.db.get_pending_products(limit=batch_size)
        
        if not pending:
            print("  没有待优化产品\n")
        else:
            optimized = self.gemini.batch_optimize(pending)
            
            for result in optimized:
                sku = result['sku']
                if 'error' in result:
                    print(f"  ✗ {sku} 优化失败: {result['error']}")
                    self.db.update_sync_status(sku, 'failed', result['error'])
                else:
                    print(f"  ✓ {sku} 优化完成")
                    self.db.update_sync_status(
                        sku,
                        'optimized',
                        optimized_title=result['optimized_title'],
                        optimized_description=result['optimized_description']
                    )
                    self.db.log_sync(sku, 'optimize', 'success', '内容已优化')
            
            print(f"  ✓ 优化完成 {len(optimized)} 个产品\n")
        
        # 步骤 3：刊登到 eBay
        print("[3/4] 刊登到 eBay...")
        optimized_products = self.db.get_products_by_status('optimized', limit=batch_size)
        
        for product in optimized_products:
            success = self._handle_listing(product)
            if success:
                print(f"  ✓ {product['sku']} 刊登成功")
            else:
                print(f"  ✗ {product['sku']} 刊登失败")
            
            time.sleep(1)  # eBay Rate Limit
        
        print(f"  ✓ 刊登完成 {len(optimized_products)} 个产品\n")
        
        # 步骤 4：更新同步时间
        print("[4/4] 更新同步状态...")
        self.db.set_config('last_sync_time', datetime.utcnow().isoformat())
        print("  ✓ 同步完成\n")
        
        print("=" * 60)
        print("完整同步流程结束")
        print("=" * 60)
    
    def run_incremental_sync(self):
        """
        增量同步（库存/价格变化）
        
        步骤：
        1. 查询 last_sync_time 之后的变化
        2. 仅更新 eBay 已刊登产品的库存/价格
        """
        print("\n开始增量同步...")
        
        # 获取已刊登产品
        listed = self.db.get_products_by_status('listed')
        
        for product in listed:
            try:
                sku = product['sku']
                
                # 从大建云仓获取最新数据
                latest = self.dajian.get_product_detail(sku)
                
                # 检查库存是否变化
                if latest.get('stock') != product['dajian_stock']:
                    new_stock = latest['stock']
                    self.ebay.update_inventory(sku, new_stock)
                    
                    self.db.save_product({
                        'sku': sku,
                        'dajian_stock': new_stock
                    })
                    
                    print(f"  ✓ {sku} 库存已更新: {new_stock}")
                    self.db.log_sync(sku, 'update', 'success', f'库存更新为 {new_stock}')
                
            except Exception as e:
                print(f"  ✗ {sku} 更新失败: {str(e)}")
                self.db.log_sync(sku, 'update', 'failed', str(e))
        
        self.db.set_config('last_sync_time', datetime.utcnow().isoformat())
        print("增量同步完成\n")
    
    def _handle_listing(self, product: dict[str, Any]) -> bool:
        """
        单个产品刊登逻辑
        
        Args:
            product: 产品数据
            
        Returns:
            是否成功
        """
        sku = product['sku']
        start_time = time.time()
        
        try:
            # 解析图片
            images = product.get('images', [])
            if isinstance(images, str):
                try:
                    images = json.loads(images)
                except:
                    images = []
            
            # 构建 PictureDetails XML
            picture_details = "<PictureDetails>"
            for img_url in images[:12]: # eBay limit 12
                picture_details += f"<PictureURL>{html.escape(img_url)}</PictureURL>"
            picture_details += "</PictureDetails>"

            # 准备数据
            title = html.escape(product['optimized_title'])
            # Description 需要 CDATA 防止 XML 解析错误，或者 escape
            description = f"<![CDATA[{product['optimized_description']}]]>"
            
            price = float(product['dajian_price'])
            quantity = product['dajian_stock']
            category_id = "20349" # Default to 'Building Materials' or 'Tools' for sandbox testing. 
                                  # Real logic should map dajian_category to eBay Category ID.

            # 构建 XML Payload (AddFixedPriceItem)
            # 使用 verified policies form create_listing_legacy.py
            xml_body = f"""
            <Item>
                <Title>{title}</Title>
                <Description>{description}</Description>
                <PrimaryCategory>
                    <CategoryID>{category_id}</CategoryID>
                </PrimaryCategory>
                <StartPrice currencyID="USD">{price}</StartPrice>
                <ConditionID>1000</ConditionID>
                <Country>US</Country>
                <Currency>USD</Currency>
                <DispatchTimeMax>3</DispatchTimeMax>
                <ListingDuration>GTC</ListingDuration>
                <ListingType>FixedPriceItem</ListingType>
                <PaymentMethods>PayPal</PaymentMethods>
                <PayPalEmailAddress>sandbox_test@paypal.com</PayPalEmailAddress>
                {picture_details}
                <ItemSpecifics>
                    <NameValueList>
                        <Name>Brand</Name>
                        <Value>Unbranded</Value>
                    </NameValueList>
                    <NameValueList>
                        <Name>Type</Name>
                        <Value>Tool</Value>
                    </NameValueList>
                </ItemSpecifics>
                <Location>San Jose</Location>
                <Quantity>{quantity}</Quantity>
                <ReturnPolicy>
                    <ReturnsAcceptedOption>ReturnsAccepted</ReturnsAcceptedOption>
                    <ReturnsWithinOption>Days_30</ReturnsWithinOption>
                    <ShippingCostPaidByOption>Buyer</ShippingCostPaidByOption>
                </ReturnPolicy>
                <ShippingDetails>
                    <ShippingServiceOptions>
                        <ShippingServicePriority>1</ShippingServicePriority>
                        <ShippingService>USPSPriority</ShippingService>
                        <ShippingServiceCost currencyID="USD">0.0</ShippingServiceCost>
                    </ShippingServiceOptions>
                    <ShippingType>Flat</ShippingType>
                </ShippingDetails>
                <Site>US</Site>
                <SKU>{sku}</SKU>
            </Item>
            """
            
            # 调用 Trading API
            response_xml = self.trading.call("AddFixedPriceItem", xml_body)
            
            # 解析响应 (简单判断)
            if "<Ack>Success</Ack>" in response_xml or "<Ack>Warning</Ack>" in response_xml:
                 # 提取 ItemID
                 import re
                 match = re.search(r"<ItemID>(\d+)</ItemID>", response_xml)
                 item_id = match.group(1) if match else "Unknown"
                 
                 print(f"  ✓ {sku} 刊登成功 (ItemID: {item_id})")
                 
                 execution_time = int((time.time() - start_time) * 1000)
                 self.db.update_sync_status(sku, 'listed', ebay_item_id=item_id)
                 self.db.log_sync(sku, 'list', 'success', f'刊登成功，ItemID: {item_id}', execution_time)
                 return True
            else:
                 print(f"  ✗ {sku} 刊登失败")
                 # 尝试提取错误信息
                 error_msg = "Unknown Error"
                 if "<LongMessage>" in response_xml:
                     error_msg = response_xml.split("<LongMessage>")[1].split("</LongMessage>")[0]
                 elif "<ShortMessage>" in response_xml:
                     error_msg = response_xml.split("<ShortMessage>")[1].split("</ShortMessage>")[0]
                     
                 print(f"    错误: {error_msg}")
                 
                 execution_time = int((time.time() - start_time) * 1000)
                 self.db.update_sync_status(sku, 'failed', error_msg)
                 self.db.log_sync(sku, 'list', 'failed', error_msg, execution_time)
                 return False

        except Exception as e:
            error_msg = str(e)
            print(f"  ✗ {sku} 异常: {error_msg}")
            
            execution_time = int((time.time() - start_time) * 1000)
            self.db.update_sync_status(sku, 'failed', error_msg)
            self.db.log_sync(sku, 'list', 'failed', error_msg, execution_time)
            
            return False
