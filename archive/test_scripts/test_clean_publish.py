"""
Clean test: Create and publish with fresh SKU
Uses updated real_ebay_client with Country field fixes
"""
import sys
from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager

def test_fresh_publish():
    """Test publication with clean data"""
    
    # Initialize
    print("[1] Initialize eBay client...")
    auth = EbayOAuthService("PRODUCTION")
    policy_mgr = EbayPolicyManager(auth)
    client = RealEbayClient(auth, policy_mgr)
    
    # Check auth
    print("[2] Check authorization...")
    try:
        token = auth.get_valid_token()
        print(f"    [OK] Token valid")
    except Exception as e:
        print(f"    [ERROR] {e}")
        return
    
    # Use new SKU to avoid conflicts
    sku = "CLEANNEW001"
    
    # Create inventory
    print(f"[3] Create inventory: {sku}...")
    product = {
        "title": "Clean Test Product",
        "description": "This is a clean test product with proper Country field",
        "image_urls": ["https://picsum.photos/300/300?random=1"],
        "quantity": 3,
        "condition": "NEW",
        "aspects": {"Brand": "TestBrand"},
        "country": "US"  # Explicitly set country in product dict
    }
    
    try:
        inv_result = client.create_or_replace_inventory_item(sku, product)
        print(f"    [OK] Inventory created: {inv_result}")
    except Exception as e:
        print(f"    [ERROR] {e}")
        return
    
    # Create offer
    print("[4] Create offer...")
    try:
        offer_result = client.create_offer(sku, 29.99)
        offer_id = offer_result.get("offerId")
        print(f"    [OK] Offer created: {offer_id}")
    except Exception as e:
        print(f"    [ERROR] {e}")
        return
    
    # Publish
    print("[5] Publish to eBay...")
    try:
        pub_result = client.publish_offer(offer_id)
        print(f"    [OK] Published: {pub_result}")
        print(f"\n[SUCCESS] Complete workflow succeeded!")
        print(f"         Listing ID: {pub_result.get('listingId')}")
        return True
    except Exception as e:
        print(f"    [ERROR] {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_fresh_publish()
    sys.exit(0 if success else 1)
