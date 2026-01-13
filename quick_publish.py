#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
快速发布脚本 - 直接使用 eBay API，无复杂依赖
"""

import argparse
import sys
import os
from dotenv import load_dotenv
from src.services.ebay_auth import EbayOAuthService

# 加载环境变量
load_dotenv()

def quick_publish(sku, title, price, quantity=1):
    """快速发布产品"""
    
    print("\n" + "=" * 60)
    print(f"发布产品: {title}")
    print("=" * 60)
    
    try:
        # 1. 获取认证
        print("[1/4] 获取认证...")
        auth = EbayOAuthService()
        token = auth.get_valid_token()
        if not token:
            print("[ERROR] 无法获取 token")
            return False
        print("[OK] 已获取 token")
        
        # 2. 创建库存
        print(f"\n[2/4] 创建库存项目...")
        import requests
        
        session = requests.Session()
        session.trust_env = False
        
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        inventory_data = {
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": int(quantity)
                }
            },
            "condition": "NEW",
            "product": {
                "title": title[:80],
                "description": f"产品: {title}\nSKU: {sku}",
                "imageUrls": [],
                "aspects": {}
            }
        }
        
        response = session.put(
            f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}",
            json=inventory_data,
            headers=headers
        )
        
        if response.status_code not in [200, 201, 204]:
            print(f"[ERROR] 库存创建失败: {response.status_code}")
            print(response.text)
            return False
        
        print(f"[OK] 库存项目已创建: {sku}")
        
        # 3. 创建 Offer
        print(f"\n[3/4] 创建 Offer...")
        
        offer_data = {
            "sku": sku,
            "marketplaceId": "EBAY_US",
            "format": "FIXED_PRICE",
            "pricingSummary": {
                "price": {
                    "currency": "USD",
                    "value": str(price)
                }
            },
            "quantityLimitPerBuyer": int(quantity),
            "listingPolicies": {
                "fulfillmentPolicyId": "321875060021",  # GIGA Shipping
                "returnPolicyId": "321875028021",        # GIGA 30days Return
                "paymentPolicyId": "321874983021"        # Combine
            }
        }
        
        response = session.post(
            'https://api.ebay.com/sell/inventory/v1/offer',
            json=offer_data,
            headers=headers
        )
        
        if response.status_code not in [200, 201]:
            print(f"[ERROR] Offer 创建失败: {response.status_code}")
            print(response.text)
            return False
        
        offer_id = response.json().get('offerId')
        print(f"[OK] Offer 已创建: {offer_id}")
        
        # 4. 发布 Offer
        print(f"\n[4/4] 发布产品到 eBay...")
        
        response = session.post(
            f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish',
            headers=headers
        )
        
        if response.status_code not in [200, 201]:
            print(f"[ERROR] 发布失败: {response.status_code}")
            print(response.text)
            return False
        
        listing_id = response.json().get('listingId')
        
        # 成功！
        print("\n" + "=" * 60)
        print("[SUCCESS] Product Published!")
        print("=" * 60)
        print(f"SKU: {sku}")
        print(f"Title: {title}")
        print(f"Price: ${price}")
        print(f"Qty: {quantity}")
        print(f"Listing ID: {listing_id}")
        print(f"Offer ID: {offer_id}")
        print("\n[OK] Product is now live on eBay!")
        print("=" * 60)
        
        return True
        
    except Exception as e:
        print(f"\n[ERROR] 异常: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def main():
    parser = argparse.ArgumentParser(description='快速发布产品到 eBay')
    parser.add_argument('--sku', help='产品 SKU')
    parser.add_argument('--title', help='产品标题')
    parser.add_argument('--price', type=float, help='产品价格')
    parser.add_argument('--qty', type=int, default=1, help='产品数量')
    parser.add_argument('--demo', action='store_true', help='演示模式')
    
    args = parser.parse_args()
    
    if args.demo or not all([args.sku, args.title, args.price]):
        # 演示数据
        sku = "DEMO-PUBLISH-001"
        title = "Demo Product - eBay Auto Publish Test"
        price = 9.99
        quantity = 5
        
        print("\n[DEMO] Demo Mode")
        print(f"  SKU: {sku}")
        print(f"  Title: {title}")
        print(f"  Price: ${price}")
        print(f"  Qty: {quantity}\n")
        
        success = quick_publish(sku, title, price, quantity)
    else:
        success = quick_publish(args.sku, args.title, args.price, args.qty)
    
    return 0 if success else 1

if __name__ == '__main__':
    sys.exit(main())
