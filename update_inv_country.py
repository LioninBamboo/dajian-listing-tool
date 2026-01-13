"""
Get detailed inventory item and try to add Country field
"""
import requests
import json
from src.services.ebay_auth import EbayOAuthService

def update_inventory_with_country():
    """Update inventory item to include Country explicitly"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US"
    }
    
    sku = "CLEANNEW001"
    
    # Get current item
    print(f"[1] Get current inventory item: {sku}")
    url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        item = response.json()
        print(f"[+] Retrieved:")
        print(json.dumps(item, indent=2)[:1000])
    else:
        print(f"[!] Error: {response.status_code}")
        print(response.text)
        return
    
    # Update with Country field
    print(f"\n[2] Updating with Country field...")
    
    # Try adding country at different levels
    updated_item = item.copy()
    
    # Level 1: Top level
    updated_item["country"] = "US"
    updated_item["itemCountry"] = "US"
    
    # Level 2: In product
    if "product" not in updated_item:
        updated_item["product"] = {}
    updated_item["product"]["country"] = "US"
    
    # PUT to update
    put_response = requests.put(url, headers=headers, json=updated_item)
    
    print(f"[*] Update response: {put_response.status_code}")
    if put_response.status_code not in [200, 204]:
        print(f"[!] Error: {put_response.text}")
    else:
        print(f"[+] Updated successfully")
        
        # Get updated item
        response = requests.get(url, headers=headers)
        updated = response.json()
        print(f"[*] Verification:")
        print(json.dumps(updated, indent=2)[:1000])

if __name__ == "__main__":
    update_inventory_with_country()
