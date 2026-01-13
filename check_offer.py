#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查已创建的 Offer 详情"""

import os
import sys
from dotenv import load_dotenv
import requests

load_dotenv()
sys.path.insert(0, '.')

from src.services.ebay_auth import EbayOAuthService

def check_offer(offer_id):
    """检查 Offer 详情"""
    
    auth = EbayOAuthService()
    token = auth.get_valid_token()
    
    if not token:
        print("[ERROR] No token")
        return
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    url = f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}'
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        print("[OK] Offer found!")
        print("\nOffer details:")
        print(f"  ID: {data.get('offerId')}")
        print(f"  SKU: {data.get('sku')}")
        print(f"  Marketplace: {data.get('marketplaceId')}")
        print(f"  Status: {data.get('status')}")
        print(f"  Listing Status: {data.get('listingStatus')}")
        print(f"  Listing Policies: {data.get('listingPolicies')}")
        print("\nFull JSON:")
        import json
        print(json.dumps(data, indent=2))
    else:
        print(f"[ERROR] {response.status_code}")
        print(response.text)

if __name__ == '__main__':
    # Check the latest offer we created
    offer_id = "104317707011"  # ROOT-COUNTRY-001
    
    if len(sys.argv) > 1:
        offer_id = sys.argv[1]
    
    print(f"Checking offer {offer_id}...")
    check_offer(offer_id)
