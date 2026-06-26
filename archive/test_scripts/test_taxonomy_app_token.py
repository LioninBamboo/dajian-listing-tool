"""Test Taxonomy API with Application Token"""
import sys
sys.path.insert(0, '.')
from src.services.ebay_auth import EbayOAuthService
import os
import requests

environment = os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION')
print(f"Environment: {environment}")

oauth = EbayOAuthService(environment)

print("\n=== Getting Application Token ===")
try:
    app_token = oauth.get_application_token()
    print(f"Application Token (first 50 chars): {app_token[:50]}...")
    print("Application Token obtained successfully!")
except Exception as e:
    print(f"Failed to get application token: {e}")
    sys.exit(1)

# Test Taxonomy API
print("\n=== Testing Taxonomy API with Application Token ===")

category_tree_id = "0"  # US
url = f"https://api.ebay.com/commerce/taxonomy/v1/category_tree/{category_tree_id}/get_category_suggestions"

headers = {
    "Authorization": f"Bearer {app_token}",
    "Accept": "application/json"
}

params = {
    "q": "Cat Litter Box"
}

print(f"URL: {url}")
print(f"Query: {params['q']}")

response = requests.get(url, headers=headers, params=params)

print(f"\nStatus Code: {response.status_code}")

if response.status_code == 200:
    data = response.json()
    suggestions = data.get("categorySuggestions", [])
    print(f"\n=== SUCCESS! Found {len(suggestions)} categories ===")
    for i, s in enumerate(suggestions[:5]):
        cat = s.get("category", {})
        cat_id = cat.get("categoryId")
        cat_name = cat.get("categoryName")
        print(f"  {i+1}. ID: {cat_id} - {cat_name}")
else:
    print(f"Error: {response.text}")
