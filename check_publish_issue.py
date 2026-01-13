#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eBay API Publish Checker
Simple diagnostics without unicode issues
"""

import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

from src.services.ebay_auth import EbayOAuthService

def check_publish_issue():
    """Check why publish fails"""
    
    print("=" * 70)
    print("[*] eBay Publish Diagnostics")
    print("=" * 70)
    
    # Get token
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = EbayOAuthService(environment)
    
    print("\n[1] Checking Token...")
    try:
        token = oauth.get_valid_token()
        print("[OK] Token valid: " + token[:30] + "...")
    except Exception as e:
        print("[ERROR] Token check failed: " + str(e))
        return
    
    # Try publish request with detailed error handling
    print("\n[2] Testing Publish Request...")
    
    offer_id = "104308333011"
    api_base = oauth.api_base
    url = api_base + "/sell/inventory/v1/offer/" + offer_id + "/publish"
    
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Content-Language": "en-US"
    }
    
    print("[*] URL: " + url)
    
    try:
        response = requests.post(url, headers=headers)
        print("[*] Response Status: " + str(response.status_code))
        
        if response.status_code != 200:
            print("[ERROR] Publish failed:")
            
            # Try to parse error
            try:
                error_data = response.json()
                print("[*] Error response:")
                for line in json.dumps(error_data, indent=2).split('\n'):
                    print("    " + line)
                
                # Check for specific errors
                if 'errors' in error_data:
                    print("\n[*] Specific errors:")
                    for error in error_data['errors']:
                        print("    - Code: " + str(error.get('errorId')))
                        print("      Message: " + error.get('message', 'No message'))
                        if 'parameters' in error:
                            print("      Parameters:")
                            for param in error['parameters']:
                                print("        - " + str(param.get('name')) + ": " + str(param.get('value')))
            except:
                print("[*] Error body: " + response.text[:500])
        else:
            print("[OK] Success!")
            print(response.json())
    
    except Exception as e:
        print("[ERROR] Request failed: " + str(e))
    
    # Check inventory
    print("\n[3] Checking Inventory Item...")
    
    sku = "DEMO-QUICK-001"
    url_inv = api_base + "/sell/inventory/v1/inventory_item/" + sku
    
    try:
        response = requests.get(url_inv, headers=headers)
        
        if response.status_code == 200:
            item_data = response.json()
            print("[OK] Inventory item found:")
            print("    - Title: " + item_data.get('product', {}).get('title', 'N/A'))
            quantity = item_data.get('availability', {}).get('shipToLocationAvailability', {}).get('quantity', 0)
            print("    - Quantity: " + str(quantity))
            print("    - Condition: " + item_data.get('condition', 'N/A'))
        else:
            print("[ERROR] Status " + str(response.status_code) + ": " + response.text[:200])
    
    except Exception as e:
        print("[ERROR] Request failed: " + str(e))
    
    # Check offer details
    print("\n[4] Checking Offer Details...")
    
    url_offer = api_base + "/sell/inventory/v1/offer/" + offer_id
    
    try:
        response = requests.get(url_offer, headers=headers)
        
        if response.status_code == 200:
            offer_data = response.json()
            print("[OK] Offer found:")
            print("    - Status: " + offer_data.get('listingStatus', 'N/A'))
            print("    - SKU: " + offer_data.get('sku', 'N/A'))
            print("    - Price: " + str(offer_data.get('pricingSummary', {}).get('price', {}).get('value')))
            print("    - Marketplace: " + offer_data.get('marketplaceId', 'N/A'))
        else:
            print("[ERROR] Status " + str(response.status_code) + ": " + response.text[:200])
    
    except Exception as e:
        print("[ERROR] Request failed: " + str(e))
    
    print("\n" + "=" * 70)
    print("[*] Common solutions:")
    print("=" * 70)
    print("""
1. Missing required fields:
   - Inventory item must have: title, description, quantity > 0
   - Offer must have: valid price, marketplaceId, SKU
   
2. Account restrictions:
   - Check seller limits on eBay
   - Some categories require additional seller information
   
3. Category requirements:
   - Some categories have specific field requirements
   - Check eBay API documentation for your category
   
4. Product data issues:
   - Description too short (min ~20 chars recommended)
   - Missing required aspects for category
   - Invalid characters in title/description
""")

if __name__ == "__main__":
    check_publish_issue()
