"""
Simple script to get published offers WITHOUT triggering SKU validation
Only fetch offers that are already published successfully
"""
import os
import json
import requests
from src.services.ebay_auth import EbayOAuthService

def get_published_offers():
    """Get only published offers using different API approach"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Try fetching with filter for published status only
    # This avoids triggering full validation on the entire collection
    url = "https://api.ebay.com/sell/inventory/v1/offer"
    params = {
        "limit": "100",
        "filter": "listingStatus:ACTIVE"
    }
    
    try:
        response = requests.get(url, headers=headers, params=params)
        print(f"[*] API Response: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            offers = data.get("offers", [])
            print(f"\n[+] Found {len(offers)} published offers")
            
            if offers:
                # Show first published offer as reference
                first_offer = offers[0]
                print(f"\n[==== FIRST PUBLISHED OFFER STRUCTURE ====]")
                print(json.dumps(first_offer, indent=2))
                
                # Save all for comparison
                with open("published_offers.json", "w") as f:
                    json.dump(offers, f, indent=2)
                print("\n[+] Saved all offers to published_offers.json")
                
                return offers
            else:
                print("[!] No published offers found")
                return []
        else:
            print(f"[!] Error: {response.status_code}")
            print(response.text)
            return []
            
    except Exception as e:
        print(f"[!] Exception: {e}")
        return []

def get_unpublished_offers():
    """Get unpublished offers we created"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Get unpublished offers only
    url = "https://api.ebay.com/sell/inventory/v1/offer"
    params = {
        "limit": "100",
        "filter": "listingStatus:NOT_LISTED"
    }
    
    try:
        response = requests.get(url, headers=headers, params=params)
        print(f"\n[*] Unpublished API Response: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            offers = data.get("offers", [])
            print(f"[+] Found {len(offers)} unpublished offers")
            
            if offers:
                first_offer = offers[0]
                print(f"\n[==== FIRST UNPUBLISHED OFFER STRUCTURE (WHAT WE CREATED) ====]")
                print(json.dumps(first_offer, indent=2))
                
                with open("unpublished_offers.json", "w") as f:
                    json.dump(offers, f, indent=2)
                print("\n[+] Saved unpublished offers to unpublished_offers.json")
                
                return offers
            else:
                print("[!] No unpublished offers found")
                return []
        else:
            print(f"[!] Error: {response.status_code}")
            print(response.text)
            return []
            
    except Exception as e:
        print(f"[!] Exception: {e}")
        return []

if __name__ == "__main__":
    print("[*] Fetching published offers (reference)...")
    published = get_published_offers()
    
    print("\n" + "="*70)
    print("[*] Fetching unpublished offers (what we created)...")
    unpublished = get_unpublished_offers()
    
    # Comparison
    if published and unpublished:
        print("\n" + "="*70)
        print("[*] COMPARISON SUMMARY")
        print(f"    Published offers: {len(published)}")
        print(f"    Unpublished offers: {len(unpublished)}")
        
        pub_first = published[0]
        unpub_first = unpublished[0]
        
        pub_keys = set(pub_first.keys())
        unpub_keys = set(unpub_first.keys())
        
        print(f"\n[*] Top-level field differences:")
        print(f"    Only in published: {pub_keys - unpub_keys}")
        print(f"    Only in unpublished: {unpub_keys - pub_keys}")
        
        # Check specific Item structure
        if "listingDetails" in pub_first and "listingDetails" in unpub_first:
            print(f"\n[*] listingDetails comparison:")
            print(f"    Published: {pub_first['listingDetails']}")
            print(f"    Unpublished: {unpub_first['listingDetails']}")
