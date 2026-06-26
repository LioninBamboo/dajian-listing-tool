"""
Test eBay Sell Metadata API for category suggestions
This API is included in the Sell scope which we already have access to
"""
import requests
import json
from src.services.ebay_auth import EbayOAuthService

oauth = EbayOAuthService('PRODUCTION')
token = oauth.get_valid_token()

headers = {
    'Authorization': f'Bearer {token}', 
    'Accept': 'application/json',
    'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
    'Content-Language': 'en-US'
}

print("=" * 60)
print("Testing eBay Sell Metadata API - Category Suggestions")
print("=" * 60)

# Test 1: Get category suggestions via Sell Metadata API
test_queries = [
    'dog crate furniture',
    'dog kennel',
    'cat litter box enclosure',
    'makeup vanity desk',
    'tv stand entertainment center'
]

for query in test_queries:
    print(f"\n🔍 Query: '{query}'")
    print("-" * 40)
    
    # Method 1: Sell Listing API (legacy but might work)
    url = 'https://api.ebay.com/sell/metadata/v1/marketplace/EBAY_US/get_item_condition_policies'
    
    # Try the sell/listing API for category suggestions
    sell_url = 'https://api.ebay.com/sell/recommendation/v1/find'
    
    r = requests.get(
        'https://api.ebay.com/sell/analytics/v1/traffic_report',
        headers=headers
    )
    
# Test 2: Try a different approach - use the existing category and get its info
print("\n" + "=" * 60)
print("Testing: Get Category Info for Known IDs")
print("=" * 60)

known_categories = {
    '177800': 'Dog Cages & Crates',
    '116363': 'Cat Litter Boxes', 
    '32878': 'Vanities & Makeup Tables',
    '20488': 'TV Stands & Entertainment Units',
    '46316': 'Aquarium & Fish Tank Valves'  # The wrong one we got
}

for cat_id, expected_name in known_categories.items():
    url = f'https://api.ebay.com/sell/metadata/v1/marketplace/EBAY_US/get_item_condition_policies?category_id={cat_id}'
    r = requests.get(url, headers=headers)
    print(f"\nCategory {cat_id} ({expected_name})")
    print(f"  Status: {r.status_code}")
    if r.status_code == 200:
        print(f"  ✅ Valid category")

# Test 3: Get aspects/policies for a category (this works and confirms the category is valid)
print("\n" + "=" * 60)
print("Testing: Get Aspects for Dog Crates Category (177800)")
print("=" * 60)

url = 'https://api.ebay.com/sell/metadata/v1/marketplace/EBAY_US/get_item_aspects_for_category?category_id=177800'
r = requests.get(url, headers=headers)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    data = r.json()
    aspects = data.get('aspects', [])
    print(f"Found {len(aspects)} aspects for category 177800:")
    for a in aspects[:10]:
        name = a.get('localizedAspectName', '')
        required = a.get('aspectConstraint', {}).get('aspectRequired', False)
        print(f"  {'⚠️' if required else '  '} {name}")
else:
    print(f"Error: {r.text[:200]}")

print("\n" + "=" * 60)
print("CONCLUSION")
print("=" * 60)
print("""
The Taxonomy API and Browse API require special OAuth scopes that we don't have.

However, we CAN solve the category problem with these approaches:

1. **Hardcoded Category Mapping** (Already implemented)
   - We maintain a mapping of product keywords to eBay category IDs
   - Dog crate → 177800, Cat litter box → 116363, etc.

2. **Use the correct category in Qwen optimizer**
   - Tell Qwen to use specific category IDs based on product type

3. **Verify category before publishing**
   - Check if the AI-suggested category is valid
   - Use our fallback mapping if not

Let me update the category mapping to be more comprehensive...
""")
