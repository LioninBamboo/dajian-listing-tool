"""
大建 API 集成综合测试脚本

测试项目中所有使用 DaJianClient 的模块:
1. DaJianClient 基础连接
2. 产品列表/详情/价格/库存 API
3. InventorySyncService 库存同步
4. TrendDiscovery 趋势发现 (如果可用)
"""
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")


def test_basic_connection():
    """测试基础 API 连接"""
    print("\n" + "=" * 60)
    print("📡 测试 1: DaJianClient 基础连接")
    print("=" * 60)
    
    from src.clients.dajian_client import DaJianClient
    
    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    
    if not client_id or not client_secret:
        print("❌ 未配置 DAJIAN_API_KEY / DAJIAN_API_SECRET")
        return False
    
    print(f"   Client ID: {client_id[:8]}...{client_id[-4:]}")
    print(f"   Base URL: {DaJianClient.DEFAULT_BASE_URL}")
    
    client = DaJianClient(client_id, client_secret)
    
    if client.test_connection():
        print("   ✅ 连接成功!")
        return True
    else:
        print("   ❌ 连接失败")
        return False


def test_product_list():
    """测试产品列表 API"""
    print("\n" + "=" * 60)
    print("📦 测试 2: 产品列表 API")
    print("=" * 60)
    
    from src.clients.dajian_client import DaJianClient
    
    client = DaJianClient(
        os.getenv("DAJIAN_API_KEY"),
        os.getenv("DAJIAN_API_SECRET")
    )
    
    try:
        products = client.get_product_list(page=1, page_size=100)
        print(f"   ✅ 获取到 {len(products)} 个产品")
        
        if products:
            sample = products[0]
            print(f"   样例 SKU: {sample.get('sku', 'N/A')}")
            print(f"   样例名称: {sample.get('productName', sample.get('name', 'N/A'))[:50]}...")
            return products[0].get('sku')  # 返回第一个 SKU 供后续测试
        return None
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return None


def test_product_detail(sku: str):
    """测试产品详情 API"""
    print("\n" + "=" * 60)
    print(f"📋 测试 3: 产品详情 API (SKU: {sku})")
    print("=" * 60)
    
    from src.clients.dajian_client import DaJianClient
    
    client = DaJianClient(
        os.getenv("DAJIAN_API_KEY"),
        os.getenv("DAJIAN_API_SECRET")
    )
    
    try:
        detail = client.get_product_detail_by_sku(sku)
        if detail:
            print(f"   ✅ 获取成功")
            print(f"   产品名: {detail.get('productName', 'N/A')[:60]}...")
            print(f"   类别: {detail.get('category', 'N/A')}")
            attrs = detail.get('attributes', [])
            if attrs:
                print(f"   属性: {len(attrs)} 项")
            return True
        else:
            print(f"   ⚠️ 无详情数据")
            return False
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False


def test_product_price(sku: str):
    """测试价格 API"""
    print("\n" + "=" * 60)
    print(f"💰 测试 4: 产品价格 API (SKU: {sku})")
    print("=" * 60)
    
    from src.clients.dajian_client import DaJianClient
    
    client = DaJianClient(
        os.getenv("DAJIAN_API_KEY"),
        os.getenv("DAJIAN_API_SECRET")
    )
    
    try:
        price = client.get_product_price(sku)
        if price:
            print(f"   ✅ 获取成功")
            print(f"   货币: {price.get('currency', 'USD')}")
            print(f"   原价: ${price.get('price', 0)}")
            print(f"   运费: ${price.get('shippingFee', 0)}")
            exc_price = price.get('exclusivePrice')
            if exc_price:
                print(f"   专享价: ${exc_price}")
            return True
        else:
            print(f"   ⚠️ 无价格数据")
            return False
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False


def test_inventory(sku: str):
    """测试库存 API"""
    print("\n" + "=" * 60)
    print(f"📊 测试 5: 库存 API (SKU: {sku})")
    print("=" * 60)
    
    from src.clients.dajian_client import DaJianClient
    
    client = DaJianClient(
        os.getenv("DAJIAN_API_KEY"),
        os.getenv("DAJIAN_API_SECRET")
    )
    
    try:
        inv = client.get_inventory_by_sku(sku)
        if inv:
            print(f"   ✅ 获取成功")
            
            buyer_inv = inv.get('buyerInventoryInfo', {})
            seller_inv = inv.get('sellerInventoryInfo', {})
            
            buyer_qty = buyer_inv.get('totalBuyerAvailableInventory', 0)
            seller_qty = seller_inv.get('sellerAvailableInventory', 0)
            
            print(f"   Buyer 可用库存: {buyer_qty}")
            print(f"   平台可售库存: {seller_qty}")
            
            # 仓库分布
            dist = seller_inv.get('sellerInventoryDistribution', [])
            if dist:
                print(f"   仓库分布: {len(dist)} 个仓库")
                for wh in dist[:3]:
                    print(f"     - {wh.get('warehouseCode')}: {wh.get('availableQtyMin', 0)}-{wh.get('availableQtyMax', 0)}")
            
            return True
        else:
            print(f"   ⚠️ 无库存数据")
            return False
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False


def test_sync_service():
    """测试 InventorySyncService"""
    print("\n" + "=" * 60)
    print("🔄 测试 6: InventorySyncService 模块")
    print("=" * 60)
    
    try:
        from src.plugins.inventory_sync.sync_service import InventorySyncService
        
        service = InventorySyncService()
        
        # 获取已发布产品
        products = service.get_published_products()
        print(f"   已发布产品: {len(products)} 个")
        
        if not products:
            print("   ⚠️ 无已发布产品，跳过库存检查测试")
            return True
        
        # 测试检查第一个产品的库存
        sku = products[0]['sku']
        print(f"   测试 SKU: {sku}")
        
        in_stock, price, shipping = service.check_dajian_stock(sku)
        
        if in_stock is not None:
            print(f"   ✅ 库存检查成功")
            print(f"   有货: {'是' if in_stock else '否'}")
            if price:
                print(f"   价格: ${price:.2f}")
            if shipping:
                print(f"   运费: ${shipping:.2f}")
            return True
        else:
            print(f"   ⚠️ 无法获取库存信息 (可能SKU不在大建仓库中)")
            return True  # 不作为失败
            
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_full_product_info(sku: str):
    """测试完整产品信息获取"""
    print("\n" + "=" * 60)
    print(f"🔍 测试 7: 完整产品信息 (SKU: {sku})")
    print("=" * 60)
    
    from src.clients.dajian_client import DaJianClient
    
    client = DaJianClient(
        os.getenv("DAJIAN_API_KEY"),
        os.getenv("DAJIAN_API_SECRET")
    )
    
    try:
        info = client.get_full_product_info(sku)
        
        detail = info.get('detail')
        price = info.get('price')
        inventory = info.get('inventory')
        
        print(f"   详情: {'✅' if detail else '❌'}")
        print(f"   价格: {'✅' if price else '❌'}")
        print(f"   库存: {'✅' if inventory else '❌'}")
        
        return bool(detail or price or inventory)
        
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False


def main():
    print("=" * 60)
    print("   大建 API 集成综合测试")
    print("=" * 60)
    
    results = {}
    
    # 测试 1: 基础连接
    results['basic'] = test_basic_connection()
    
    if not results['basic']:
        print("\n❌ 基础连接失败，无法继续测试")
        return
    
    # 测试 2: 产品列表
    sample_sku = test_product_list()
    results['product_list'] = sample_sku is not None
    
    if sample_sku:
        # 测试 3-5: 使用样例 SKU
        results['product_detail'] = test_product_detail(sample_sku)
        results['product_price'] = test_product_price(sample_sku)
        results['inventory'] = test_inventory(sample_sku)
        results['full_info'] = test_full_product_info(sample_sku)
    
    # 测试 6: 同步服务
    results['sync_service'] = test_sync_service()
    
    # 汇总
    print("\n" + "=" * 60)
    print("📊 测试汇总")
    print("=" * 60)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for name, ok in results.items():
        status = "✅ 通过" if ok else "❌ 失败"
        print(f"   {name}: {status}")
    
    print(f"\n   总计: {passed}/{total} 通过")
    
    if passed == total:
        print("\n🎉 所有测试通过!")
    else:
        print("\n⚠️ 部分测试失败，请检查日志")


if __name__ == "__main__":
    main()
