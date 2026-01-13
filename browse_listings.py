"""
Use eBay Browse API to see published listings structure
This API might not trigger the inventory SKU validation issues
"""
import requests
import json
from src.services.ebay_auth import EbayOAuthService

def browse_published_listings():
    """Use Browse API to get published listing structure"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
    }
    
    # Search for items I'm selling
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    
    # Search parameters
    params = {
        "q": "seller:xiaoting_8888",  # Your seller ID if available
        "limit": "10",
        "sort": "newlyListed"
    }
    
    print("[*] Searching for your published listings via Browse API...")
    
    try:
        response = requests.get(url, headers=headers, params=params)
        print(f"[*] Response code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            items = data.get("itemSummaries", [])
            print(f"[+] Found {len(items)} published listings")
            
            if items:
                item = items[0]
                print(f"\n[==== SAMPLE PUBLISHED LISTING ====]")
                print(json.dumps(item, indent=2)[:2000])
        else:
            print(f"[!] Error: {response.status_code}")
            print(response.text[:500])
            
    except Exception as e:
        print(f"[!] Exception: {e}")

if __name__ == "__main__":
    browse_published_listings()
