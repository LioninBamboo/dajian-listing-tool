import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / '.env')
from src.services.ebay_auth import EbayOAuthService
oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
token = oauth.get_valid_token()
headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

# Check inventory item
r = requests.get('https://api.ebay.com/sell/inventory/v1/inventory_item/W215P423356', headers=headers)
print('Inventory status:', r.status_code)
if r.status_code == 200:
    data = r.json()
    avail = data.get('availability', {})
    print('Availability:', avail)
    title = data.get('product', {}).get('title', 'N/A')[:80]
    print('Title:', title)

# Check offers
r2 = requests.get('https://api.ebay.com/sell/inventory/v1/offer', headers=headers, params={'sku': 'W215P423356'})
print()
print('Offers status:', r2.status_code)
if r2.status_code == 200:
    offers = r2.json().get('offers', [])
    for o in offers:
        oid = o.get('offerId')
        st = o.get('status')
        lid = o.get('listing', {}).get('listingId')
        price = o.get('pricingSummary', {}).get('price', {})
        print(f'  Offer {oid}: status={st}, listingId={lid}, price={price}')
