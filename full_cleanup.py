"""
Get all inventory items and identify which ones have invalid SKUs (with hyphens)
"""
import requests
import json
from src.services.ebay_auth import EbayOAuthService

def cleanup_all_invalid_skus():
    """Get all inventory items and delete those with hyphens"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Get all inventory items
    url = "https://api.ebay.com/sell/inventory/v1/inventory_item"
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        print(f"[!] Failed to get inventory items: {response.status_code}")
        print(response.text)
        return
    
    data = response.json()
    items = data.get("inventoryItems", [])
    
    print(f"[*] Found {len(items)} total inventory items")
    
    # Identify invalid SKUs (those with hyphens)
    invalid_skus = [item["sku"] for item in items if "-" in item["sku"]]
    
    print(f"[*] Found {len(invalid_skus)} items with hyphens (invalid):")
    for sku in invalid_skus:
        print(f"    - {sku}")
    
    # Delete them
    if invalid_skus:
        print(f"\n[*] Deleting {len(invalid_skus)} invalid SKUs...")
        
        for sku in invalid_skus:
            delete_url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"
            
            try:
                del_response = requests.delete(delete_url, headers=headers)
                
                if del_response.status_code in [200, 204]:
                    print(f"    [OK] Deleted: {sku}")
                else:
                    print(f"    [ER] {del_response.status_code}: {sku}")
                    
            except Exception as e:
                print(f"    [EX] {str(e)[:80]}")
        
        print(f"\n[+] Cleanup complete! Invalid SKUs removed.")
    
    # Show valid SKUs remaining
    valid_skus = [item["sku"] for item in items if "-" not in item["sku"]]
    print(f"\n[*] Valid SKUs remaining ({len(valid_skus)}):")
    for sku in valid_skus:
        print(f"    - {sku}")

if __name__ == "__main__":
    cleanup_all_invalid_skus()
