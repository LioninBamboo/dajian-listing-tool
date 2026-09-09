"""测试真实产品发布到 eBay"""
import os
import sys
import sqlite3
import json
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from src.services.ebay_auth import EbayOAuthService
from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_policy_manager import EbayPolicyManager

# 从数据库获取产品
conn = sqlite3.connect('ebay_collection.db')
c = conn.cursor()
c.execute('SELECT sku, title, optimization, images, suggested_price, stock FROM collected_products WHERE sku LIKE "%WhiteWalnut%"')
row = c.fetchone()
conn.close()

sku = row[0]
title = row[1]
opt = json.loads(row[2]) if row[2] else {}
images = json.loads(row[3]) if row[3] else []
suggested_price = row[4]
stock = row[5]

print(f"测试发布 SKU: {sku}")
print(f"Title: {opt.get('title', title)[:80]}")
print(f"Price: ${suggested_price}")
print(f"Images: {len(images)}")

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
    print("\n正在创建 Inventory Item...")
    result = ebay_client.create_or_replace_inventory_item(
        sku=sku,
        product={
            "title": opt.get("title", title)[:80],
            "description": opt.get("description", "<p>Test</p>"),
            "image_urls": images[:12],
            "price": suggested_price,
            "quantity": stock or 1,
            "condition": "NEW",
            "aspects": opt.get("aspects", {"Brand": ["AquaVerve"]})
        }
    )
    print(f"✅ Inventory Item 创建成功: {result}")
    
    print("\n正在创建 Offer...")
    offer = ebay_client.create_offer(
        sku=sku,
        price=suggested_price,
        category_id=opt.get("categoryId")
    )
    print(f"✅ Offer 创建成功: {offer}")
    
except Exception as e:
    print(f"❌ 失败: {e}")
    import traceback
    traceback.print_exc()
