"""
测试 eBay API 连接
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import requests
import urllib3
urllib3.disable_warnings()

from src.services.ebay_auth import EbayOAuthService

def main():
    print("=" * 60)
    print("eBay API 连接测试")
    print("=" * 60)
    
    env = os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION')
    print(f"环境: {env}")
    
    oauth = EbayOAuthService(env)
    print(f"✓ eBay Authorized: {oauth.is_authorized()}")
    
    if not oauth.is_authorized():
        print("✗ 未授权，需要先完成 OAuth 授权流程")
        return
    
    token = oauth.get_valid_token()
    print(f"✓ Got valid token: {token[:30]}...")
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    # 测试 Inventory API
    print("\n--- 测试 Inventory API ---")
    r = requests.get(
        'https://api.ebay.com/sell/inventory/v1/inventory_item?limit=5', 
        headers=headers, 
        timeout=15, 
        verify=False
    )
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"✓ Total inventory items: {data.get('total', 0)}")
        if 'inventoryItems' in data:
            for item in data['inventoryItems'][:3]:
                print(f"  - SKU: {item.get('sku')}")
    else:
        print(f"✗ Error: {r.text[:200]}")
    
    # 测试 Account API (获取 policies)
    print("\n--- 测试 Account API (Fulfillment Policies) ---")
    r = requests.get(
        'https://api.ebay.com/sell/account/v1/fulfillment_policy?marketplace_id=EBAY_US',
        headers=headers,
        timeout=15,
        verify=False
    )
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        policies = data.get('fulfillmentPolicies', [])
        print(f"✓ Fulfillment policies: {len(policies)}")
        for p in policies[:3]:
            print(f"  - {p.get('name')} (ID: {p.get('fulfillmentPolicyId')})")
    else:
        print(f"✗ Error: {r.text[:200]}")
    
    # 测试 Taxonomy API
    print("\n--- 测试 Taxonomy API ---")
    r = requests.get(
        'https://api.ebay.com/commerce/taxonomy/v1/category_tree/0',
        headers=headers,
        timeout=15,
        verify=False
    )
    print(f"Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"✓ Category tree: {data.get('categoryTreeId')} - {data.get('categoryTreeVersion')}")
    else:
        print(f"✗ Error: {r.text[:200]}")
    
    print("\n" + "=" * 60)
    print("eBay API 测试完成")
    print("=" * 60)

if __name__ == "__main__":
    main()
