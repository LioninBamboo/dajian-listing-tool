#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '.')
from dotenv import load_dotenv
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
import requests

load_dotenv()

# Create a simple test offer
auth = EbayOAuthService()
token = auth.get_valid_token()

if not token:
    print("[ERROR] No token")
    sys.exit(1)

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

# Create session
session = requests.Session()
session.trust_env = False

# First, create inventory
print("[1] Creating inventory...")
sku = "TEST-OFFER-RESPONSE-001"
inventory_payload = {
    "availability": {
        "shipToLocationAvailability": {
            "quantity": 5,
            "shipToLocation": "US"
        }
    },
    "condition": "NEW",
    "itemLocationCountry": "US",
    "product": {
        "title": "Test Product",
        "description": "Test description",
        "imageUrls": ["https://picsum.photos/300/300?random=1"],
        "aspects": {}
    }
}

inv_resp = session.put(
    f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}",
    json=inventory_payload,
    headers=headers
)

if inv_resp.status_code not in [200, 201, 204]:
    print(f"[ERROR] Inventory creation failed: {inv_resp.status_code}")
    print(inv_resp.text)
    sys.exit(1)

print("[OK] Inventory created")

# Now create offer and inspect response
print("\n[2] Creating offer...")
offer_payload = {
    "sku": sku,
    "marketplaceId": "EBAY_US",
    "format": "FIXED_PRICE",
    "pricingSummary": {
        "price": {
            "value": "9.99",
            "currency": "USD"
        }
    },
    "listingPolicies": {
        "fulfillmentPolicyId": "321897899021",
        "returnPolicyId": "321896608021",
        "paymentPolicyId": "321896606021"
    },
    "countryCode": "US"
}

offer_resp = session.post(
    'https://api.ebay.com/sell/inventory/v1/offer',
    json=offer_payload,
    headers=headers
)

print(f"Status: {offer_resp.status_code}")
print(f"\nFull Response:")
print(offer_resp.text)

if offer_resp.status_code in [200, 201]:
    data = offer_resp.json()
    offer_id = data.get('offerId')
    print(f"\n[OK] Offer created: {offer_id}")
    print(f"\nOffer response keys: {list(data.keys())}")
    
    # Try to immediately get the offer
    print(f"\n[3] Retrieving created offer...")
    get_resp = session.get(
        f'https://api.ebay.com/sell/inventory/v1/offer/{offer_id}',
        headers=headers
    )
    
    if get_resp.status_code == 200:
        get_data = get_resp.json()
        print(f"Status from GET: {get_data.get('status')}")
        print(f"ListingStatus from GET: {get_data.get('listingStatus')}")
        print(f"All fields: {list(get_data.keys())}")
