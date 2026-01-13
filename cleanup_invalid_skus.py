"""
Delete all inventory items with invalid SKUs (containing hyphens)
This is blocking our API queries
"""
import requests
from src.services.ebay_auth import EbayOAuthService

def delete_invalid_skus():
    """Delete inventory items with hyphens in SKU"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # List of known bad SKUs (with hyphens)
    bad_skus = [
        "ROOT-COUNTRY-001",
        "DEMO-FAST-001", 
        "TEST-PUBLISH-001",
        "DEMO-PUBLISH-001",
        "QUICK-PUBLISH-001",
        "TEST-OFFER-001",
        "ALPHA-TEST-001",
        "BETA-TEST-001",
        "GAMMA-TEST-001",
        "DELTA-TEST-001",
        "DEMO-SKU-001",
        "TEST-SKU-001",
        "QA-SKU-001"
    ]
    
    print(f"[*] Attempting to delete {len(bad_skus)} inventory items with invalid SKUs...")
    
    deleted_count = 0
    
    for sku in bad_skus:
        # DELETE /inventory_item/{sku}
        url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"
        
        try:
            response = requests.delete(url, headers=headers)
            
            if response.status_code in [200, 204]:
                print(f"[OK] Deleted: {sku}")
                deleted_count += 1
            elif response.status_code == 404:
                print(f"[NO] Not found: {sku}")
            else:
                print(f"[ER] Error deleting {sku}: {response.status_code}")
                print(f"     Response: {response.text[:200]}")
                
        except Exception as e:
            print(f"[EX] Exception deleting {sku}: {str(e)[:100]}")
    
    print(f"\n[+] Deleted {deleted_count} items")
    print("[*] Now you should be able to query offers without SKU validation errors")

if __name__ == "__main__":
    delete_invalid_skus()
