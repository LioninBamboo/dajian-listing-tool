"""
Try using eBay Trading API (XML-based) to get listing structure
This API might bypass the Inventory API's SKU validation issues
"""
import requests
from src.services.ebay_auth import EbayOAuthService
import xml.etree.ElementTree as ET

def get_listings_via_trading_api():
    """Get My eBay Selling items"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
        "X-EBAY-API-APP-NAME": os.getenv("EBAY_APP_ID", ""),
        "X-EBAY-API-CERT-NAME": os.getenv("EBAY_CERT_ID", ""),
        "X-EBAY-API-DEV-NAME": os.getenv("EBAY_DEV_ID", ""),
        "X-EBAY-API-COMPATIBILITY-LEVEL": "967"
    }
    
    # Try the XML Trading API endpoint
    url = "https://api.ebay.com/ws/api.dll"
    
    # This endpoint doesn't accept Bearer token, but let's try
    print("[*] Attempting Trading API...")
    
    try:
        response = requests.get(url, headers=headers)
        print(f"[*] Response: {response.status_code}")
        print(response.text[:500])
    except Exception as e:
        print(f"[!] Exception: {e}")

def get_my_seller_info():
    """Get seller account info via Account API"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Try Account API
    url = "https://api.ebay.com/sell/account/v1/seller-profile"
    
    print("[*] Attempting to get seller profile...")
    
    try:
        response = requests.get(url, headers=headers)
        print(f"[*] Response: {response.status_code}")
        if response.status_code == 200:
            print("[+] Got seller info!")
            import json
            print(json.dumps(response.json(), indent=2)[:1000])
        else:
            print(f"[!] Error: {response.text[:300]}")
    except Exception as e:
        print(f"[!] Exception: {e}")

if __name__ == "__main__":
    import os
    print("[ATTEMPT 1] Trading API:")
    get_listings_via_trading_api()
    
    print("\n[ATTEMPT 2] Seller Profile:")
    get_my_seller_info()
