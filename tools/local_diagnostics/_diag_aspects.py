"""Quick diagnostic for failed category updates"""
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
from src.clients.real_ebay_client import RealEbayClient

oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
pm = EbayPolicyManager(oauth)
client = RealEbayClient(oauth, pm)
token = oauth.get_valid_token()

def get_required_aspects(cat_id):
    resp = requests.get(
        'https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/get_item_aspects_for_category',
        params={'category_id': str(cat_id)},
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
        timeout=30
    )
    if resp.status_code == 200:
        return [a['localizedAspectName'] for a in resp.json().get('aspects', [])
                if a.get('aspectConstraint', {}).get('aspectRequired', False)]
    return []

# Check Bar Stools 183316
print("=== Category 183316 (Bar Stools) REQUIRED aspects ===")
for name in get_required_aspects('183316'):
    print(f"  {name}")

# Check Ice Chests 79691
print("\n=== Category 79691 (Ice Chests & Coolers) REQUIRED aspects ===")
for name in get_required_aspects('79691'):
    print(f"  {name}")

# Check what W3916P426287 has
print("\n=== W3916P426287 current aspects ===")
inv = client.get_inventory_item('W3916P426287')
if inv:
    pkg = inv.get('packageWeightAndSize', {})
    print(f"  Weight: {pkg.get('weight', {})}")
    aspects = inv.get('product', {}).get('aspects', {})
    for k, v in sorted(aspects.items()):
        print(f"  {k}: {v}")

# Check what offers look like for W3916P426287
print("\n=== W3916P426287 offer ===")
offers = client.get_offers_by_sku('W3916P426287')
if offers:
    o = offers[0]
    print(f"  offerId: {o.get('offerId')}")
    print(f"  categoryId: {o.get('categoryId')}")
    print(f"  status: {o.get('status')}")
    print(f"  listingId: {o.get('listing', {}).get('listingId')}")
