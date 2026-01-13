"""Check eBay offer status"""
import requests
from src.services.ebay_auth import EbayOAuthService

auth = EbayOAuthService('PRODUCTION')
token = auth.get_valid_token()

headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

# Check offer status
offer_id = '104329311011'
url = f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}'

resp = requests.get(url, headers=headers)
print(f'Status: {resp.status_code}')
if resp.status_code == 200:
    data = resp.json()
    print(f"SKU: {data.get('sku')}")
    print(f"Offer Status: {data.get('status')}")
    listing = data.get('listing', {})
    print(f"Listing ID: {listing.get('listingId', 'N/A')}")
    print(f"Category: {data.get('categoryId')}")
else:
    print(resp.text[:300])
