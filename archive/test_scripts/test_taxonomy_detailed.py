"""Test Taxonomy API with detailed error logging"""
import sys
sys.path.insert(0, '.')
from src.services.ebay_auth import EbayOAuthService
import os
import requests

environment = os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION')
print(f"Environment: {environment}")

oauth = EbayOAuthService(environment)

if not oauth.is_authorized():
    print("eBay not authorized!")
    sys.exit(1)

print("eBay Authorized: Yes")

# Get user token
token = oauth.get_valid_token()
print(f"Token (first 50 chars): {token[:50]}...")

# Test Taxonomy API
print("\n=== Testing Taxonomy API ===")

category_tree_id = "0"  # US
url = f"https://api.ebay.com/commerce/taxonomy/v1/category_tree/{category_tree_id}/get_category_suggestions"

headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

params = {
    "q": "Cat Litter Box"
}

print(f"URL: {url}")
print(f"Query: {params['q']}")

response = requests.get(url, headers=headers, params=params)

print(f"\nStatus Code: {response.status_code}")
print(f"Response Headers: {dict(response.headers)}")
print(f"\nResponse Body:")
print(response.text[:1000] if len(response.text) > 1000 else response.text)

# If 403, try with Application Token instead
if response.status_code == 403:
    print("\n\n=== Trying with Application Token ===")
    
    # Get application token (client credentials)
    import base64
    
    client_id = os.getenv('EBAY_CLIENT_ID')
    client_secret = os.getenv('EBAY_CLIENT_SECRET')
    
    if client_id and client_secret:
        credentials = f"{client_id}:{client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        
        token_url = "https://api.ebay.com/identity/v1/oauth2/token"
        token_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded}"
        }
        token_data = {
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope"
        }
        
        token_resp = requests.post(token_url, headers=token_headers, data=token_data)
        print(f"App Token Request Status: {token_resp.status_code}")
        
        if token_resp.status_code == 200:
            app_token = token_resp.json().get('access_token')
            print(f"App Token (first 50): {app_token[:50]}...")
            
            # Try Taxonomy API with app token
            app_headers = {
                "Authorization": f"Bearer {app_token}",
                "Accept": "application/json"
            }
            
            app_response = requests.get(url, headers=app_headers, params=params)
            print(f"\nTaxonomy with App Token - Status: {app_response.status_code}")
            print(f"Response: {app_response.text[:500]}")
        else:
            print(f"Failed to get app token: {token_resp.text}")
