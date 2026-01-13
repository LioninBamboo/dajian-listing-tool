#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '.')
from dotenv import load_dotenv
from src.services.ebay_auth import EbayOAuthService
import requests
import json

load_dotenv()

auth = EbayOAuthService()
token = auth.get_valid_token()

headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

# Try to get offers without filter
url = 'https://api.ebay.com/sell/inventory/v1/offer?limit=100'

print("Getting all offers...")
response = requests.get(url, headers=headers)

if response.status_code == 200:
    data = response.json()
    offers = data.get('offers', [])
    
    print(f"Found {len(offers)} total offers")
    
    # Find published ones
    published = [o for o in offers if o.get('status') == 'PUBLISHED']
    unpublished = [o for o in offers if o.get('status') != 'PUBLISHED']
    
    print(f"Published: {len(published)}")
    print(f"Unpublished: {len(unpublished)}")
    
    if published:
        print("\n[PUBLISHED OFFER - Active Listing Reference]")
        print("=" * 80)
        pub = published[0]
        print(json.dumps(pub, indent=2))
    
    if unpublished:
        print("\n[UNPUBLISHED OFFER - Our Created One]")
        print("=" * 80)
        unpub = unpublished[0]
        print(json.dumps(unpub, indent=2))
    
    # Save for detailed inspection
    with open('all_offers_data.json', 'w') as f:
        json.dump(data, f, indent=2)
    print("\nSaved full data to: all_offers_data.json")
else:
    print(f"Error: {response.status_code}")
    print(response.text)
