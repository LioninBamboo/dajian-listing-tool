"""
简化版本：直接创建新的 offer (带 Location) 并发布
这次使用全新的 SKU 来避免任何缓存问题
"""
import requests
import json
from datetime import datetime
from src.services.ebay_auth import EbayOAuthService

def create_and_publish_with_location():
    """创建并发布一个 offer，使用 Inventory Location"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US"
    }
    
    # 生成一个全新的 SKU (确保没有缓存问题)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    sku = f"PUBLISH{timestamp}"[:50]  # Max 50 chars, alphanumeric only
    
    merchant_location_key = "DAJIAN_WAREHOUSE"
    
    print(f"[*] Creating complete listing with Location...")
    print(f"[*] SKU: {sku}")
    print(f"[*] Location: {merchant_location_key}\n")
    
    # Step 1: Create inventory item
    # Use category 293 (Cell Phone Accessories) - simpler aspect requirements
    print(f"[1] Creating Inventory Item...")
    inventory_payload = {
        "availability": {
            "shipToLocationAvailability": {
                "quantity": 5
            }
        },
        "condition": "NEW",
        "product": {
            "title": "Universal Phone Stand Holder - Demo Product",
            "description": "This product was created with proper Location information containing Country field.",
            "aspects": {
                "Brand": ["Unbranded"],
                "Type": ["Stand"],
                "Compatible Brand": ["Universal"],
                "Color": ["Black"]
            },
            "imageUrls": ["https://picsum.photos/300/300?random=1"]
        }
    }
    
    inv_url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"
    inv_response = requests.put(inv_url, headers=headers, json=inventory_payload)
    
    if inv_response.status_code not in [200, 201, 204]:
        print(f"[ER] Failed to create inventory item: {inv_response.status_code}")
        print(f"     {inv_response.text[:500]}")
        return
    
    print(f"[OK] Inventory item created\n")
    
    # Step 2: Create offer (with Location!)
    # Category 35190 = Cell Phone Mounts & Holders - simpler requirements
    print(f"[2] Creating Offer (with Location)...")
    offer_payload = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": 5,
        "categoryId": "35190",
        "pricingSummary": {
            "price": {
                "currency": "USD",
                "value": "29.99"
            }
        },
        "listingDuration": "GTC",
        "listingPolicies": {
            "fulfillmentPolicyId": "321897899021",
            "returnPolicyId": "321896608021",
            "paymentPolicyId": "321896606021"
        },
        "merchantLocationKey": merchant_location_key  # <-- KEY: This provides the Country!
    }
    
    offer_url = "https://api.ebay.com/sell/inventory/v1/offer"
    offer_response = requests.post(offer_url, headers=headers, json=offer_payload)
    
    if offer_response.status_code != 201:
        print(f"[ER] Failed to create offer: {offer_response.status_code}")
        print(f"     {offer_response.text[:500]}")
        return
    
    offer_id = offer_response.json().get("offerId")
    print(f"[OK] Offer created: {offer_id}\n")
    
    # Step 3: Publish offer
    print(f"[3] Publishing Offer to create Listing...")
    pub_url = f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish"
    pub_response = requests.post(pub_url, headers=headers)
    
    print(f"[*] Publish response: {pub_response.status_code}")
    
    if pub_response.status_code == 200:
        data = pub_response.json()
        listing_id = data.get("listingId")
        print(f"\n[SUCCESS] LISTING PUBLISHED!")
        print(f"==================================")
        print(f"Listing ID: {listing_id}")
        print(f"SKU: {sku}")
        print(f"View at: https://www.ebay.com/itm/{listing_id}")
        print(f"==================================\n")
        return listing_id
    else:
        print(f"[ER] Publish failed: {pub_response.status_code}")
        try:
            print(f"     Response text: {pub_response.text[:800]}")
        except:
            print(f"     (Error with response encoding)")
        
        # Show error details
        if pub_response.status_code == 400:
            try:
                error_data = pub_response.json()
                errors = error_data.get("errors", [])
                for err in errors:
                    print(f"\n  Error {err.get('errorId')}: {err.get('message')}")
                    print(f"  Details: {err.get('longMessage')}")
            except:
                pass
        
        return None

if __name__ == "__main__":
    create_and_publish_with_location()
