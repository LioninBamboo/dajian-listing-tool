"""
Force-delete all offers to clear out any with invalid SKUs
This is a nuclear option to reset the system
"""
import requests
from src.services.ebay_auth import EbayOAuthService

def nuke_all_offers():
    """Delete ALL offers to reset"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Get all offers with a huge limit
    url = "https://api.ebay.com/sell/inventory/v1/offer"
    params = {"limit": "200"}  # Very high limit
    
    print("[*] Attempting to retrieve all offers (high limit)...")
    
    try:
        response = requests.get(url, headers=headers, params=params)
        print(f"[*] Response code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            offers = data.get("offers", [])
            print(f"[+] Found {len(offers)} offers")
            
            if offers:
                print("[*] Sample offer structure:")
                print(f"    Offer ID: {offers[0].get('offerId')}")
                print(f"    Status: {offers[0].get('status')}")
                print(f"    SKU: {offers[0].get('sku')}")
                
                # Try to delete first offer
                if offers:
                    first_offer_id = offers[0].get("offerId")
                    print(f"\n[*] Attempting to delete offer: {first_offer_id}")
                    
                    del_url = f"https://api.ebay.com/sell/inventory/v1/offer/{first_offer_id}"
                    del_response = requests.delete(del_url, headers=headers)
                    print(f"[*] Delete response: {del_response.status_code}")
                    if del_response.text:
                        print(f"    {del_response.text[:200]}")
        else:
            print(f"[!] Error: {response.status_code}")
            print(f"    {response.text[:500]}")
            
    except Exception as e:
        print(f"[!] Exception: {e}")

if __name__ == "__main__":
    nuke_all_offers()
