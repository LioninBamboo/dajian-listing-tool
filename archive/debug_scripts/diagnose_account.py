"""
Diagnose: Check if the problem is account-level or API-level
"""
import requests
from src.services.ebay_auth import EbayOAuthService

def check_account_settings():
    """Check account-level settings"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Try to get account info
    endpoints = [
        ("Account Info", "https://api.ebay.com/sell/account/v1/seller-profile"),
        ("Policies", "https://api.ebay.com/sell/account/v1/policies"),
        ("Site Details", "https://api.ebay.com/commerce/catalog/v1/site"),
    ]
    
    for name, url in endpoints:
        print(f"\n[*] {name}:")
        print(f"    URL: {url}")
        try:
            response = requests.get(url, headers=headers)
            print(f"    Status: {response.status_code}")
            if response.status_code in [200, 201]:
                import json
                data = response.json()
                print(f"    Data: {json.dumps(data, indent=2)[:500]}")
            else:
                print(f"    Error: {response.text[:300]}")
        except Exception as e:
            print(f"    Exception: {e}")

def check_existing_listings():
    """Try to get info on existing listings"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
    }
    
    # Try to get sold/active items
    urls = [
        ("Active Listings", "https://api.ebay.com/sell/inventory/v1/inventory_item"),
    ]
    
    for name, url in urls:
        print(f"\n[*] {name}:")
        try:
            response = requests.get(url, headers=headers)
            print(f"    Status: {response.status_code}")
            if response.status_code == 200:
                import json
                data = response.json()
                items = data.get("inventoryItems", [])
                print(f"    Count: {len(items)}")
                if items:
                    print(f"    First item SKU: {items[0].get('sku')}")
                    print(f"    First item data (partial):")
                    sample = {k: v for k, v in items[0].items() if k not in ['product']}
                    print(f"    {sample}")
            else:
                print(f"    Error: {response.text[:300]}")
        except Exception as e:
            print(f"    Exception: {e}")

if __name__ == "__main__":
    print("[DEBUG] Checking account configuration...")
    check_account_settings()
    
    print("\n\n[DEBUG] Checking inventory...")
    check_existing_listings()
