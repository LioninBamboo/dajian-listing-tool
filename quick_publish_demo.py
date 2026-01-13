#!/usr/bin/env python3
"""
快速产品发布演示脚本

演示如何使用 eBay API 快速发布产品：
1. 创建库存项目
2. 创建 Offer
3. 发布到 eBay

运行：python quick_publish_demo.py
"""

import os
import sys
from dotenv import load_dotenv

# Load env
load_dotenv()

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from clients.real_ebay_client import create_real_ebay_client

def quick_publish_demo():
    """Quick demo of publishing a product"""
    
    print("=" * 70)
    print("🚀 eBay 快速发布演示")
    print("=" * 70)
    
    # Initialize client
    print("\n1️⃣  初始化 eBay 客户端...")
    try:
        client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        print("   ✅ 客户端已初始化")
    except Exception as e:
        print(f"   ❌ 初始化失败: {e}")
        return False
    
    # Check authorization
    print("\n2️⃣  检查授权状态...")
    if not client.oauth.is_authorized():
        print("   ❌ 未授权！请先运行授权流程")
        print(f"   授权 URL: {client.oauth.get_authorization_url()}")
        return False
    print("   ✅ 已授权")
    
    # Demo product
    demo_sku = "DEMO-QUICK-001"
    demo_product = {
        "title": "演示产品 - 高质量 LED 灯泡",
        "description": """
这是一个演示产品。

特点：
- 高效能 LED 技术
- 节能环保
- 长寿命（50000 小时）
- 多色温选择

规格：
- 功率: 10W
- 色温: 3000K (温白色)
- 亮度: 800 流明
- 寿命: 50000 小时

包装：
- 数量: 1个
- 重量: 0.1kg

新品质量保证。

完整的售后支持。
        """.strip(),
        "price": 15.99,
        "quantity": 10,
        "condition": "NEW",
        "image_urls": [
            "https://example.com/image1.jpg",
            "https://example.com/image2.jpg"
        ],
        "aspects": {
            "Brand": ["演示品牌"],
            "Color": ["白色"],
            "Type": ["灯泡"]
        }
    }
    
    # Step 1: Create inventory item
    print(f"\n3️⃣  创建库存项目...")
    print(f"   SKU: {demo_sku}")
    print(f"   标题: {demo_product['title']}")
    print(f"   价格: ${demo_product['price']}")
    print(f"   数量: {demo_product['quantity']}")
    
    try:
        inv_result = client.create_or_replace_inventory_item(demo_sku, demo_product)
        print(f"   ✅ 库存项目创建/更新成功")
    except Exception as e:
        print(f"   ⚠️  库存项目操作: {e}")
        inv_result = None
    
    # Step 2: Create offer
    print(f"\n4️⃣  创建 Offer...")
    try:
        offer_result = client.create_offer(demo_sku, demo_product["price"])
        offer_id = offer_result.get("offerId")
        
        if offer_result.get("status") == "EXISTING":
            print(f"   ℹ️  Offer 已存在: {offer_id}")
        else:
            print(f"   ✅ Offer 创建成功: {offer_id}")
    except Exception as e:
        print(f"   ❌ 创建 Offer 失败: {e}")
        return False
    
    # Step 3: Publish offer
    print(f"\n5️⃣  发布 Offer 到 eBay...")
    try:
        publish_result = client.publish_offer(offer_id)
        listing_id = publish_result.get("listingId")
        print(f"   ✅ 发布成功！")
        print(f"   Listing ID: {listing_id}")
    except Exception as e:
        print(f"   ❌ 发布失败: {e}")
        return False
    
    # Summary
    print("\n" + "=" * 70)
    print("✅ 发布成功！")
    print("=" * 70)
    print(f"\n📊 产品信息:")
    print(f"   SKU: {demo_sku}")
    print(f"   标题: {demo_product['title']}")
    print(f"   Listing ID: {listing_id}")
    print(f"   价格: ${demo_product['price']}")
    print(f"   数量: {demo_product['quantity']}")
    
    print(f"\n🔗 你可以在 eBay 上查看你的产品!")
    print(f"\n💡 下一步:")
    print(f"   1. 访问 https://www.ebay.com 搜索你的产品")
    print(f"   2. 修改 demo_sku 和产品信息发布新产品")
    print(f"   3. 运行此脚本多次发布多个产品")
    
    return True

def publish_custom_product(sku, title, price, quantity=1, description=""):
    """发布自定义产品"""
    
    print(f"\n🚀 发布产品: {sku}")
    print(f"   标题: {title}")
    print(f"   价格: ${price}")
    print(f"   数量: {quantity}")
    
    try:
        client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        
        # Check auth
        if not client.oauth.is_authorized():
            print("❌ 未授权")
            return False
        
        # Create product dict
        product = {
            "title": title,
            "description": description or title,
            "price": price,
            "quantity": quantity,
            "condition": "NEW",
            "aspects": {}
        }
        
        # Create inventory
        client.create_or_replace_inventory_item(sku, product)
        print("   ✅ 库存项目创建")
        
        # Create offer
        offer_result = client.create_offer(sku, price)
        offer_id = offer_result.get("offerId")
        print(f"   ✅ Offer 创建: {offer_id}")
        
        # Publish
        publish_result = client.publish_offer(offer_id)
        listing_id = publish_result.get("listingId")
        print(f"   ✅ 发布成功: {listing_id}")
        
        return True
    except Exception as e:
        print(f"   ❌ 失败: {e}")
        return False

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="eBay 产品发布脚本")
    parser.add_argument("--demo", action="store_true", help="运行演示（默认）")
    parser.add_argument("--sku", help="产品 SKU")
    parser.add_argument("--title", help="产品标题")
    parser.add_argument("--price", type=float, help="产品价格")
    parser.add_argument("--quantity", type=int, default=1, help="产品数量")
    parser.add_argument("--description", help="产品描述")
    
    args = parser.parse_args()
    
    if args.sku and args.title and args.price:
        # Publish custom product
        success = publish_custom_product(
            sku=args.sku,
            title=args.title,
            price=args.price,
            quantity=args.quantity,
            description=args.description or ""
        )
        sys.exit(0 if success else 1)
    else:
        # Run demo
        success = quick_publish_demo()
        sys.exit(0 if success else 1)
