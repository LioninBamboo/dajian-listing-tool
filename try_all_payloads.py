"""
Final attempt: Try different publish payload structures
Based on eBay documentation for error 25002
"""
import requests
import json
from src.services.ebay_auth import EbayOAuthService

def publish_with_full_item_details():
    """
    Try publishing with a complete Item structure in the payload
    This is a Hail Mary approach based on the error message
    """
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    offer_id = "104323171011"
    sku = "CLEANNEW001"
    
    # Different payload structures to try
    payloads = [
        {
            "name": "Empty body",
            "data": {}
        },
        {
            "name": "With itemCountry",
            "data": {"itemCountry": "US"}
        },
        {
            "name": "With item.country",
            "data": {
                "item": {
                    "country": "US"
                }
            }
        },
        {
            "name": "With location country",
            "data": {
                "item": {
                    "location": {
                        "country": "US"
                    }
                }
            }
        },
        {
            "name": "With shipToLocations",
            "data": {
                "item": {
                    "shipToLocations": {
                        "countryCode": "US"
                    }
                }
            }
        },
        {
            "name": "Complex nested",
            "data": {
                "Item": {
                    "Country": "US",
                    "LocationCountry": "US"
                },
                "item": {
                    "country": "US"
                },
                "itemCountry": "US"
            }
        }
    ]
    
    url = f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish"
    
    print(f"[*] Testing publish payload structures for offer: {offer_id}\n")
    
    for i, test_case in enumerate(payloads, 1):
        print(f"[Test {i}] {test_case['name']}")
        print(f"   Payload: {json.dumps(test_case['data'])}")
        
        try:
            if test_case['data']:
                response = requests.post(url, headers=headers, json=test_case['data'])
            else:
                response = requests.post(url, headers=headers)
            
            if response.status_code == 200:
                print(f"   [SUCCESS!] {response.status_code}")
                result = response.json()
                print(f"   Listing ID: {result.get('listingId')}")
                return True
            else:
                error_data = response.json() if response.text else {}
                error_msg = error_data.get("errors", [{}])[0].get("message", "Unknown error")
                print(f"   [FAILED] {response.status_code}: {error_msg[:80]}")
        except Exception as e:
            print(f"   [EXCEPTION] {str(e)[:80]}")
        
        print()
    
    return False

if __name__ == "__main__":
    print("[*] Attempting different publish payload structures...\n")
    success = publish_with_full_item_details()
    
    if success:
        print("\n[SUCCESS] Found working payload structure!")
    else:
        print("\n[CONCLUSION] None of the standard payload structures work.")
        print("[ANALYSIS] This suggests either:")
        print("  1. Your eBay account has special requirements")
        print("  2. You need to configure Country via eBay dashboard first")
        print("  3. This is a bug or limitation in eBay's Inventory API")
