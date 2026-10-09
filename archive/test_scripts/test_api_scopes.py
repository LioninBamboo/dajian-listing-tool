"""
Check OAuth scopes and test different API approaches
"""
import requests
import json
from src.services.ebay_auth import EbayOAuthService

oauth = EbayOAuthService('PRODUCTION')
token = oauth.get_valid_token()

print("Testing with current OAuth token...")
print("=" * 60)

# Test different endpoints
endpoints = [
    ('Taxonomy API', 'https://api.ebay.com/commerce/taxonomy/v1/category_tree/0'),
    ('Browse API', 'https://api.ebay.com/buy/browse/v1/item_summary/search?q=dog&limit=1'),
    ('Sell Inventory API', 'https://api.ebay.com/sell/inventory/v1/inventory_item?limit=1'),
]

headers = {
    'Authorization': f'Bearer {token}', 
    'Accept': 'application/json',
    'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
    'Content-Language': 'en-US'
}

for name, url in endpoints:
    r = requests.get(url, headers=headers)
    print(f"\n{name}")
    print(f"  URL: {url}")
    print(f"  Status: {r.status_code}")
    if r.status_code != 200:
        try:
            err = r.json()
            print(f"  Error: {json.dumps(err, indent=2)[:300]}")
        except:
            print(f"  Response: {r.text[:200]}")

# Test with Client Credentials Grant (app-only token)
print("\n" + "=" * 60)
print("Testing with Client Credentials (App Token)...")
print("=" * 60)

from dotenv import load_dotenv
import os
import base64

load_dotenv()
client_id = os.getenv('EBAY_CLIENT_ID')
client_secret = os.getenv('EBAY_CLIENT_SECRET')

# Get app token
credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
token_url = 'https://api.ebay.com/identity/v1/oauth2/token'

app_token_response = requests.post(
    token_url,
    headers={
        'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': f'Basic {credentials}'
    },
    data={
        'grant_type': 'client_credentials',
        'scope': 'https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/buy.browse'
    }
)

if app_token_response.status_code == 200:
    app_token = app_token_response.json().get('access_token')
    print(f"Got app token: {app_token[:30]}...")
    
    # Test Taxonomy and Browse with app token
    app_headers = {
        'Authorization': f'Bearer {app_token}', 
        'Accept': 'application/json',
        'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'
    }
    
    # Test Taxonomy
    r = requests.get(
        'https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/get_category_suggestions',
        headers=app_headers,
        params={'q': 'dog crate'}
    )
    print(f"\nTaxonomy API with app token: {r.status_code}")
    if r.status_code == 200:
        suggestions = r.json().get('categorySuggestions', [])
        print(f"Found {len(suggestions)} category suggestions for 'dog crate':")
        for s in suggestions[:5]:
            cat = s.get('category', {})
            print(f"  ID: {cat.get('categoryId')} - {cat.get('categoryName')}")
    else:
        print(f"  Error: {r.text[:200]}")
        
    # Test Browse
    r = requests.get(
        'https://api.ebay.com/buy/browse/v1/item_summary/search',
        headers=app_headers,
        params={'q': 'dog crate', 'limit': 3}
    )
    print(f"\nBrowse API with app token: {r.status_code}")
    if r.status_code == 200:
        items = r.json().get('itemSummaries', [])
        print(f"Found {len(items)} items")
        for item in items[:3]:
            cats = item.get('categories', [])
            print(f"  Item: {item.get('title', '')[:50]}...")
            for c in cats:
                print(f"    Category: {c.get('categoryId')} - {c.get('categoryName')}")
    else:
        print(f"  Error: {r.text[:200]}")
else:
    print(f"Failed to get app token: {app_token_response.status_code}")
    print(app_token_response.text[:300])
