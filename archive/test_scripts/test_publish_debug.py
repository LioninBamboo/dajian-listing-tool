"""测试发布到 eBay"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from src.services.ebay_auth import EbayOAuthService
from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_policy_manager import EbayPolicyManager

# 测试 SKU
sku = "WS216S00001-WhiteWalnutMDFMetalDog"

print(f"测试发布 SKU: {sku}")

# 获取 OAuth
environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
oauth = EbayOAuthService(environment)

if not oauth.is_authorized():
    print("❌ eBay 未授权")
    exit(1)

print("✅ eBay 已授权")

# 创建客户端
policy_manager = EbayPolicyManager(oauth)
ebay_client = RealEbayClient(oauth, policy_manager)

# 测试创建 inventory item
try:
    result = ebay_client.create_or_replace_inventory_item(
        sku=sku,
        product={
            "title": "Test Product Title",
            "description": "<p>Test Description</p>",
            "image_urls": ["https://via.placeholder.com/500"],
            "price": 100.00,
            "quantity": 1,
            "condition": "NEW",
            "aspects": {"Brand": ["AquaVerve"]}
        }
    )
    print(f"✅ 创建成功: {result}")
except Exception as e:
    print(f"❌ 创建失败: {e}")
    import traceback
    traceback.print_exc()
