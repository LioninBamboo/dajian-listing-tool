#!/usr/bin/env python3
"""
eBay Publish 错误诊断脚本

诊断为什么 publish 失败 (400 Bad Request)
"""

import os
import sys
import requests
import json
from dotenv import load_dotenv

load_dotenv()

# Direct import without using src package
from src.services.ebay_auth import EbayOAuthService

def diagnose_publish_error():
    """诊断发布失败的原因"""
    
    print("=" * 70)
    print("🔍 eBay Publish 错误诊断")
    print("=" * 70)
    
    # Get OAuth service
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = EbayOAuthService(environment)
    
    # Check token
    print("\n1️⃣  检查 Token...")
    try:
        token = oauth.get_valid_token()
        print(f"   ✅ Token 有效: {token[:30]}...")
    except Exception as e:
        print(f"   ❌ Token 无效: {e}")
        return
    
    # Try to publish with error details
    print("\n2️⃣  尝试发布 Offer...")
    
    offer_id = "104308333011"  # From previous test
    api_base = oauth.api_base
    url = f"{api_base}/sell/inventory/v1/offer/{offer_id}/publish"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US"
    }
    
    print(f"   URL: {url}")
    print(f"   Method: POST")
    
    try:
        response = requests.post(url, headers=headers)
        print(f"   Response Status: {response.status_code}")
        
        if response.status_code != 200:
            print(f"\n   ❌ 错误信息:")
            print(f"   {response.text}")
            
            try:
                error_data = response.json()
                print(f"\n   📋 错误详情 (JSON):")
                print(json.dumps(error_data, indent=2, ensure_ascii=False))
            except:
                pass
        else:
            print(f"   ✅ 成功!")
            print(f"   {response.json()}")
    
    except Exception as e:
        print(f"   ❌ 请求失败: {e}")
    
    # Check offer status
    print("\n3️⃣  检查 Offer 状态...")
    url = f"{api_base}/sell/inventory/v1/offer/{offer_id}"
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            offer_data = response.json()
            print(f"   ✅ Offer 状态:")
            print(json.dumps(offer_data, indent=2, ensure_ascii=False))
        else:
            print(f"   ❌ 获取失败: {response.text}")
    
    except Exception as e:
        print(f"   ❌ 请求失败: {e}")
    
    # Check inventory item
    print("\n4️⃣  检查库存项目...")
    sku = "DEMO-QUICK-001"
    url = f"{api_base}/sell/inventory/v1/inventory_item/{sku}"
    
    try:
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            item_data = response.json()
            print(f"   ✅ 库存项目状态:")
            print(f"   Title: {item_data.get('product', {}).get('title')}")
            print(f"   Quantity: {item_data.get('availability', {}).get('shipToLocationAvailability', {}).get('quantity')}")
            print(f"   Condition: {item_data.get('condition')}")
        else:
            print(f"   ℹ️  库存项目: {response.status_code}")
    
    except Exception as e:
        print(f"   ⚠️  请求失败: {e}")
    
    # Common solutions
    print("\n" + "=" * 70)
    print("💡 常见解决方案:")
    print("=" * 70)
    
    print("""
1. 检查库存项目：
   - 是否有足够的库存数量（quantity > 0）
   - 是否有有效的价格
   - 是否有产品标题和描述

2. 检查 Offer：
   - 是否正确设置了 marketplace ID
   - 是否有有效的价格
   - 是否有 listing policies（可选）

3. 账户设置：
   - 检查 eBay 账户是否有发布权限
   - 检查是否有适当的 feedback（某些类别需要）
   - 检查是否超过每日/每月上架限额

4. eBay API 限制：
   - 某些产品类别有额外的要求
   - 某些字段可能有格式要求
   - 查看 eBay API 文档了解具体要求
    """)

if __name__ == "__main__":
    diagnose_publish_error()
