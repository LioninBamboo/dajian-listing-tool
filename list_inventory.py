"""
Try to list inventory items directly
"""
import requests
from src.services.ebay_auth import EbayOAuthService

def list_inventory_items():
    """List all inventory items"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Try to get inventory items with a simpler approach
    url = "https://api.ebay.com/sell/inventory/v1/inventory_item"
    
    try:
        response = requests.get(url, headers=headers)
        print(f"[*] API Response: {response.status_code}")
        print(f"[*] Response: {response.text[:1000]}")
        
    except Exception as e:
        print(f"[!] Exception: {e}")

if __name__ == "__main__":
    list_inventory_items()
