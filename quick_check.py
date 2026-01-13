#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '.')
from dotenv import load_dotenv
from src.services.ebay_auth import EbayOAuthService
import requests

load_dotenv()

auth = EbayOAuthService()
token = auth.get_valid_token()

headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

offers = ['104317707011', '104317371011', '104317329011']

print("Offer Status Check:")
print("-" * 70)

for offer_id in offers:
    response = requests.get(f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}', headers=headers)
    if response.status_code == 200:
        data = response.json()
        sku = data.get('sku')
        status = data.get('status')
        listing_status = data.get('listingStatus')
        print(f"Offer {offer_id}")
        print(f"  SKU: {sku}")
        print(f"  Status: {status}")
        print(f"  ListingStatus: {listing_status}")
        print()
    else:
        print(f"Offer {offer_id}: ERROR {response.status_code}")
