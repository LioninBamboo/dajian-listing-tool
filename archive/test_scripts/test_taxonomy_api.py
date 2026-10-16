"""
Test eBay Taxonomy API to get correct category IDs
"""
import requests
from src.services.ebay_auth import EbayOAuthService

oauth = EbayOAuthService('PRODUCTION')
token = oauth.get_valid_token()

headers = {
    'Authorization': f'Bearer {token}', 
    'Accept': 'application/json',
    'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'
}

# Test queries
queries = [
    'dog crate',
    'dog kennel furniture',
    'cat litter box enclosure',
    'vanity desk makeup',
    'tv stand'
]

print("=" * 60)
print("Testing eBay Taxonomy API - Category Suggestions")
print("=" * 60)

for query in queries:
    url = 'https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/get_category_suggestions'
    params = {'q': query}
    
    r = requests.get(url, headers=headers, params=params)
    print(f"\nQuery: '{query}'")
    print(f"Status: {r.status_code}")
    
    if r.status_code == 200:
        data = r.json()
        suggestions = data.get('categorySuggestions', [])
        print(f"Found {len(suggestions)} suggestions:")
        for s in suggestions[:3]:
            cat = s.get('category', {})
            ancestors = s.get('categoryTreeNodeAncestors', [])
            path = ' > '.join([a.get('categoryName', '') for a in reversed(ancestors)])
            print(f"  ID: {cat.get('categoryId')}")
            print(f"  Name: {cat.get('categoryName')}")
            print(f"  Path: {path}")
            print()
    elif r.status_code == 403:
        print("  403 Forbidden - Need to use Browse API instead")
        
        # Try Browse API
        browse_url = 'https://api.ebay.com/buy/browse/v1/item_summary/search'
        browse_params = {'q': query, 'limit': 1}
        r2 = requests.get(browse_url, headers=headers, params=browse_params)
        print(f"  Browse API status: {r2.status_code}")
        if r2.status_code == 200:
            items = r2.json().get('itemSummaries', [])
            if items:
                item = items[0]
                cats = item.get('categories', [])
                print(f"  Found category from similar item:")
                for c in cats:
                    print(f"    {c.get('categoryId')}: {c.get('categoryName')}")
    else:
        print(f"  Error: {r.text[:200]}")

print("\n" + "=" * 60)
