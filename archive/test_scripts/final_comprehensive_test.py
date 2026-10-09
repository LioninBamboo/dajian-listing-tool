#!/usr/bin/env python
"""
FINAL COMPREHENSIVE TEST
Tests all steps and provides complete diagnostic output
"""

import json
import sys
from src.services.ebay_auth import EbayOAuthService
from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_policy_manager import EbayPolicyManager

def main():
    print("""
╔════════════════════════════════════════════════════════════════════════════╗
║         eBay LISTING PUBLISH - COMPREHENSIVE DIAGNOSTIC TEST              ║
╚════════════════════════════════════════════════════════════════════════════╝
    """)
    
    # [1] Initialize
    print("[STEP 1] Initialize eBay Client")
    try:
        auth = EbayOAuthService("PRODUCTION")
        policy_mgr = EbayPolicyManager(auth)
        client = RealEbayClient(auth, policy_mgr)
        print("    Status: OK")
    except Exception as e:
        print(f"    Status: FAILED - {e}")
        return False
    
    # [2] Check Auth
    print("\n[STEP 2] Verify Authorization Token")
    try:
        token = auth.get_valid_token()
        print(f"    Status: OK")
        print(f"    Token: {token[:50]}...")
    except Exception as e:
        print(f"    Status: FAILED - {e}")
        return False
    
    # [3] Create Inventory
    print("\n[STEP 3] Create Inventory Item")
    sku = "FINAL_TEST_SKU"
    product = {
        "title": "Final Test Product",
        "description": "Testing to resolve Item.Country error",
        "image_urls": ["https://picsum.photos/300/300"],
        "quantity": 1,
        "condition": "NEW",
        "aspects": {}
    }
    
    try:
        result = client.create_or_replace_inventory_item(sku, product)
        print(f"    Status: OK")
        print(f"    SKU: {sku}")
        print(f"    Result: {result}")
    except Exception as e:
        print(f"    Status: FAILED")
        print(f"    Error: {e}")
        return False
    
    # [4] Create Offer
    print("\n[STEP 4] Create Offer")
    try:
        offer = client.create_offer(sku, 29.99)
        offer_id = offer.get("offerId")
        print(f"    Status: OK")
        print(f"    Offer ID: {offer_id}")
        print(f"    Result: {offer}")
    except Exception as e:
        print(f"    Status: FAILED")
        print(f"    Error: {e}")
        return False
    
    # [5] Publish - This is where it fails
    print("\n[STEP 5] Publish Offer")
    try:
        result = client.publish_offer(offer_id)
        print(f"    Status: OK - PUBLISH SUCCESSFUL!")
        print(f"    Listing ID: {result.get('listingId')}")
        print(f"    Result: {result}")
        return True
    except Exception as e:
        print(f"    Status: FAILED")
        print(f"    Error: {str(e)[:200]}")
        
        # Extract error details
        import requests
        try:
            if hasattr(e, 'response') and e.response is not None:
                error_data = e.response.json()
                if 'errors' in error_data:
                    for err in error_data['errors']:
                        print(f"\n    eBay Error Details:")
                        print(f"      Error ID: {err.get('errorId')}")
                        print(f"      Message: {err.get('message')}")
                        if 'parameters' in err:
                            print(f"      Parameters: {err.get('parameters')}")
        except:
            pass
        
        return False

def summary():
    print("""
╔════════════════════════════════════════════════════════════════════════════╗
║                           TROUBLESHOOTING STEPS                           ║
╚════════════════════════════════════════════════════════════════════════════╝

If Steps 1-4 passed but Step 5 failed with "Item.Country" error:

1. LOGIN TO EBAY SELLER CENTER:
   https://sellercentral.ebay.com

2. CHECK THESE SETTINGS:
   - Account Settings -> Selling Location -> Country: "United States"
   - Inventory -> Default Item Location Country: "US"
   - Make sure NO FIELD IS EMPTY

3. AFTER CHANGING SETTINGS:
   - Wait 5 minutes
   - Log out and log back in
   - Try again: python final_comprehensive_test.py

4. IF STILL FAILING:
   - There may be a deeper account restriction
   - Contact eBay: https://sellercentral.ebay.com/community
   - Provide: Offer ID, Error 25002, and these steps

5. ALTERNATIVE:
   - Use eBay web interface to create first listing
   - Then replicate structure in code

═══════════════════════════════════════════════════════════════════════════════
    """)

if __name__ == "__main__":
    success = main()
    summary()
    
    sys.exit(0 if success else 1)
