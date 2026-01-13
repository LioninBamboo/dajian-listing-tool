#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check detailed status of created Offers"""

import os
import sys
from dotenv import load_dotenv
import requests
import json

load_dotenv()
sys.path.insert(0, '.')

from src.services.ebay_auth import EbayOAuthService

def check_offer_detailed(offer_id):
    """检查 Offer 的完整详情"""
    
    auth = EbayOAuthService()
    token = auth.get_valid_token()
    
    if not token:
        print("[ERROR] 无法获取 token")
        return None
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    url = f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}'
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"[ERROR] {response.status_code}: {response.text}")
        return None

def check_inventory(sku):
    """检查库存项目的状态"""
    
    auth = EbayOAuthService()
    token = auth.get_valid_token()
    
    if not token:
        print("[ERROR] 无法获取 token")
        return None
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    url = f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"[ERROR] {response.status_code}")
        return None

def main():
    print("=" * 70)
    print("检查已创建的 Offer 和库存状态")
    print("=" * 70)
    
    # 检查最近创建的几个 Offer
    offers = [
        "104317707011",    # ROOT-COUNTRY-001
        "104317371011",    # ITEM-LOCATION-COUNTRY-001
        "104317329011",    # EMPTY-BODY-001
    ]
    
    for offer_id in offers:
        print(f"\n[Check] Offer ID: {offer_id}")
        print("-"*70)
        
        data = check_offer_detailed(offer_id)
        if data:
            print(f"SKU: {data.get('sku')}")
            print(f"Status: {data.get('status')}")
            print(f"Listing Status: {data.get('listingStatus')}")
            print(f"Price: {data.get('pricingSummary', {}).get('price', {}).get('value')}")
            print(f"Policies Configured: {bool(data.get('listingPolicies'))}")
            
            # 检查完整性指标
            required_fields = ['sku', 'marketplaceId', 'pricingSummary', 'listingPolicies']
            missing = [f for f in required_fields if not data.get(f)]
            
            if missing:
                print(f"Missing fields: {missing}")
            else:
                print("All required fields: OK")
    
    # Also check inventory
    print("\n\n[Check Inventory Items]")
    print("="*70)
    
    skus = ["ROOT-COUNTRY-001", "ITEM-LOCATION-COUNTRY-001", "EMPTY-BODY-001"]
    
    for sku in skus:
        print(f"\n[Check] SKU: {sku}")
        print("-"*70)
        
        data = check_inventory(sku)
        if data:
            print(f"SKU: {data.get('sku')}")
            print(f"Condition: {data.get('condition')}")
            print(f"Inventory Status: {data.get('status', 'N/A')}")
            if 'availability' in data:
                print(f"Availability Configured: OK")
            if 'product' in data:
                print(f"Product Info Configured: OK")
        print()

if __name__ == '__main__':
    main()
