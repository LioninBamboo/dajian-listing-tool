#!/usr/bin/env python3
"""
批量发布所有 READY 状态的产品到 eBay

使用带自动类目匹配的发布函数，避免 Invalid category 错误
"""
import os
import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import sqlite3
import logging
from datetime import datetime
from sqlalchemy.orm.attributes import flag_modified

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(PROJECT_ROOT / "logs" / f"batch_publish_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def get_ready_products():
    """获取所有待发布的产品"""
    conn = sqlite3.connect("ebay_collection.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    cur.execute("""
        SELECT * FROM collected_products 
        WHERE status IN ('READY', 'READY_TO_PUBLISH')
        ORDER BY sku
    """)
    
    products = []
    for row in cur.fetchall():
        product = dict(row)
        # Parse JSON fields
        for field in ['optimization', 'cost_breakdown', 'images', 'videos', 'specs', 'attributes', 'logs']:
            if product.get(field):
                try:
                    product[field] = json.loads(product[field])
                except:
                    pass
        products.append(product)
    
    conn.close()
    return products


def publish_single_product(product: dict) -> dict:
    """发布单个产品（使用 auto-category 版本，带智能重试）"""
    from src.clients.real_ebay_client import RealEbayClient
    from src.services.ebay_policy_manager import EbayPolicyManager
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_category_matcher import EbayCategoryMatcher
    from src.services.ebay_publisher import EbayPublisher
    
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = EbayOAuthService(environment)
    
    if not oauth.is_authorized():
        return {"status": "error", "message": "eBay 未授权"}
    
    sku = product['sku']
    max_retries = 2  # 最多重试2次
    
    for attempt in range(max_retries):
        try:
            policy_manager = EbayPolicyManager(oauth)
            ebay_client = RealEbayClient(oauth, policy_manager)
            
            opt_data = product.get('optimization', {})
            if not opt_data:
                return {"status": "error", "message": "产品未优化"}
            
            final_price = product.get('suggested_price', 0) or product.get('price', 0)
            if not final_price or final_price <= 0:
                return {"status": "error", "message": f"价格无效: {final_price}"}
            
            existing_aspects = opt_data.get("aspects", {"Brand": ["AquaVerve"]})
            
            # 自动类目匹配
            category_matcher = EbayCategoryMatcher(oauth)
            category_id, category_name, completed_aspects = category_matcher.get_category_and_aspects(
                opt_data.get("title", product.get('title', '')),
                existing_aspects,
                product.get('description', '')
            )
            
            if not category_id:
                category_id = opt_data.get("categoryId")
                completed_aspects = existing_aspects
            
            if not category_id:
                return {"status": "error", "message": "无法确定产品类目"}
            
            # 使用 CATEGORY_REQUIRED_ASPECTS 补全必填属性
            if category_id in EbayPublisher.CATEGORY_REQUIRED_ASPECTS:
                cat_config = EbayPublisher.CATEGORY_REQUIRED_ASPECTS[category_id]
                for req_aspect, default_values in cat_config.get("defaults", {}).items():
                    if req_aspect not in completed_aspects or not completed_aspects[req_aspect]:
                        completed_aspects[req_aspect] = [default_values] if isinstance(default_values, str) else default_values
                        if attempt > 0:  # 重试时记录补充的属性
                            logger.info(f"  [AUTO-FIX] 补充缺失属性: {req_aspect} = {completed_aspects[req_aspect]}")
            
            logger.info(f"  类目: {category_id} ({category_name})")
            
            # 1. Create Inventory Item
            # 使用智能HTML截断器，保持结构完整
            description = opt_data.get("description", product.get('description', ''))
            description = smart_truncate_html(description, max_length=50000, min_length=45000)
            
            ebay_client.create_or_replace_inventory_item(
                sku=sku,
                product={
                    "title": opt_data.get("title", product.get('title', ''))[:80],
                    "description": description,
                    "image_urls": product.get('images', [])[:12],
                    "price": final_price,
                    "quantity": 1,  # 固定1件
                    "condition": "NEW",
                    "aspects": completed_aspects
                }
            )
            
            # 2. Create or Get Existing Offer
            offer_id = None
            try:
                offer = ebay_client.create_offer(
                    sku=sku,
                    price=final_price,
                    category_id=category_id
                )
                
                if not offer or not offer.get("offerId"):
                    return {"status": "error", "message": "创建 Offer 失败"}
                
                offer_id = offer["offerId"]
            except Exception as offer_err:
                # 如果 offer 已存在，尝试获取现有 offer
                if "already exists" in str(offer_err):
                    logger.info("  Offer 已存在，尝试获取现有 offer...")
                    # 从错误消息中提取 offer ID
                    import re
                    match = re.search(r'"offerId","value":"(\d+)"', str(offer_err))
                    if match:
                        offer_id = match.group(1)
                        logger.info(f"  使用现有 Offer: {offer_id}")
                        # 尝试更新现有 offer
                        try:
                            ebay_client.update_offer_category(offer_id, category_id)
                            logger.info(f"  已更新 Offer 类目和属性")
                        except Exception as update_err:
                            logger.warning(f"  更新 Offer 失败: {update_err}")
                    else:
                        raise offer_err
                else:
                    raise offer_err
            
            if not offer_id:
                return {"status": "error", "message": "无法获取 Offer ID"}
            
            # 3. Publish
            publish_result = ebay_client.publish_offer(offer_id)
            listing_id = publish_result.get("listingId")
            
            if listing_id:
                # 4. 尝试上传视频（如果有）
                videos = product.get('videos', [])
                if videos and len(videos) > 0:
                    try:
                        from src.services.ebay_video_uploader import EbayVideoUploader
                        video_uploader = EbayVideoUploader(oauth)
                        video_title = opt_data.get("title", "")[:50]
                        video_id = video_uploader.upload_video_sync(videos[0], sku, video_title)
                        if video_id:
                            logger.info(f"  视频已上传: {video_id}")
                    except Exception as ve:
                        logger.warning(f"  视频上传失败: {ve}")
                
                return {
                    "status": "success",
                    "message": f"发布成功! Listing ID: {listing_id}",
                    "listing_id": listing_id,
                    "offer_id": offer_id,
                    "category_id": category_id
                }
            else:
                return {"status": "error", "message": f"发布失败: {publish_result}"}
                
        except Exception as e:
            error_msg = str(e)
            
            # 检查是否是可重试的错误
            if attempt < max_retries - 1:
                # 解析错误，看是否缺少必填属性
                if "is missing" in error_msg or "Invalid category" in error_msg:
                    logger.warning(f"  [重试 {attempt + 1}/{max_retries}] 检测到缺失属性或类目错误，尝试自动修复...")
                    
                    # 提取缺失的属性名（如 "Firmness is missing"）
                    import re
                    match = re.search(r'The item specific (\w+) is missing', error_msg)
                    if match:
                        missing_aspect = match.group(1)
                        logger.info(f"  [AUTO-FIX] 检测到缺失属性: {missing_aspect}")
                        
                        # 尝试从 CATEGORY_REQUIRED_ASPECTS 获取默认值
                        if category_id in EbayPublisher.CATEGORY_REQUIRED_ASPECTS:
                            defaults = EbayPublisher.CATEGORY_REQUIRED_ASPECTS[category_id].get("defaults", {})
                            if missing_aspect in defaults:
                                # 更新产品的 optimization.aspects
                                if 'optimization' not in product:
                                    product['optimization'] = {}
                                if 'aspects' not in product['optimization']:
                                    product['optimization']['aspects'] = {}
                                product['optimization']['aspects'][missing_aspect] = [defaults[missing_aspect]]
                                logger.info(f"  [AUTO-FIX] 已添加: {missing_aspect} = {defaults[missing_aspect]}")
                    
                    continue  # 重试
            
            # 最后一次重试失败或不可重试错误
            import traceback
            logger.error(traceback.format_exc())
            return {"status": "error", "message": error_msg}


def update_product_status(sku: str, listing_id: str):
    """更新产品状态为 PUBLISHED"""
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct
    
    db = SessionLocal()
    product = db.query(CollectedProduct).filter_by(sku=sku).first()
    if product:
        product.status = "PUBLISHED"
        product.listing_id = listing_id
        product.logs = (product.logs or []) + [
            f"Published at {datetime.utcnow().isoformat()}"
        ]
        flag_modified(product, 'logs')
        db.commit()
    db.close()


def main():
    logger.info("="*60)
    logger.info("开始批量发布产品到 eBay")
    logger.info("="*60)
    
    products = get_ready_products()
    
    if not products:
        logger.info("没有待发布的产品")
        return
    
    logger.info(f"共 {len(products)} 个产品待发布\n")
    
    success_count = 0
    fail_count = 0
    skip_count = 0
    
    for i, product in enumerate(products, 1):
        sku = product['sku']
        title = product.get('title', '')[:60]
        
        logger.info(f"[{i}/{len(products)}] 发布: {sku}")
        logger.info(f"  标题: {title}...")
        logger.info(f"  价格: ${product.get('suggested_price', 0):.2f}")
        
        result = publish_single_product(product)
        
        if result['status'] == 'success':
            success_count += 1
            listing_id = result.get('listing_id', result.get('offer_id'))
            update_product_status(sku, listing_id)
            logger.info(f"  ✅ 成功: {result.get('message')}\n")
        else:
            fail_count += 1
            logger.error(f"  ❌ 失败: {result.get('message')}\n")
    
    logger.info("="*60)
    logger.info("批量发布完成!")
    logger.info(f"成功: {success_count} 个")
    logger.info(f"失败: {fail_count} 个")
    logger.info(f"跳过: {skip_count} 个")
    logger.info("="*60)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"批量发布异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        logging.shutdown()
