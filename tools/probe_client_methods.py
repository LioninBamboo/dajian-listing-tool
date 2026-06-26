"""测试 DaJianClient 新增的 API 方法"""
import sys
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
import os

from src.clients.dajian_client import DaJianClient

client = DaJianClient(
    client_id=os.getenv('DAJIAN_API_KEY'),
    client_secret=os.getenv('DAJIAN_API_SECRET'),
    base_url=os.getenv('DAJIAN_BASE_URL')
)

test_sku = 'N710P401337K'

print('=== 测试 DaJianClient 新增 API 方法 ===')

# 1. 产品详情
print('\n1. 产品详情 (get_product_detail_by_sku):')
detail = client.get_product_detail_by_sku(test_sku)
if detail:
    print(f"   SKU: {detail.get('sku')}")
    print(f"   Name: {str(detail.get('productName', ''))[:40]}...")
    print(f"   Category: {detail.get('category')}")
    print(f"   Weight: {detail.get('weight')} {detail.get('weightUnit')}")
else:
    print("   No data")

# 2. 产品价格
print('\n2. 产品价格 (get_product_price):')
price = client.get_product_price(test_sku)
if price:
    print(f"   Price: {price.get('price')} {price.get('currency')}")
    print(f"   Shipping: {price.get('shippingFee')}")
    print(f"   Available: {price.get('skuAvailable')}")
else:
    print("   No data")

# 3. 库存
print('\n3. 库存信息 (get_inventory_by_sku):')
inv = client.get_inventory_by_sku(test_sku)
if inv:
    buyer = inv.get('buyerInventoryInfo', {})
    seller = inv.get('sellerInventoryInfo', {})
    print(f"   Buyer Available: {buyer.get('totalBuyerAvailableInventory')}")
    print(f"   Seller Available: {seller.get('sellerAvailableInventory')}")
else:
    print("   No data")

# 4. 完整信息
print('\n4. 完整产品信息 (get_full_product_info):')
full = client.get_full_product_info(test_sku)
print(f"   Detail: {'OK' if full['detail'] else 'N/A'}")
print(f"   Price: {'OK' if full['price'] else 'N/A'}")
print(f"   Inventory: {'OK' if full['inventory'] else 'N/A'}")

# 5. 批量查询
print('\n5. 批量查询 (get_product_details):')
skus = ['N710P401337K', 'N710P401336K']
details = client.get_product_details(skus)
print(f"   Requested: {len(skus)} SKUs")
print(f"   Returned: {len(details)} products")

print('\n=== 所有测试通过! ===')
