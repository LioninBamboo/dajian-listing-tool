"""
创建 Inventory Location (包含国家信息)，然后发布 offer

关键发现：eBay Inventory API 需要一个带有完整地址的 Location
包括：city, stateOrProvince, country
或：postalCode, country

这就是 "No <Item.Country>" 错误的真正原因！
"""
import requests
import json
from src.services.ebay_auth import EbayOAuthService

def create_inventory_location():
    """创建一个 Inventory Location (卖家位置)"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US"
    }
    
    # 创建位置的唯一键
    merchant_location_key = "DAJIAN_WAREHOUSE"
    
    # 创建位置的有效地址 (带国家!)
    # 大建云仓发货地址：Los Angeles, CA
    location_payload = {
        "location": {
            "address": {
                "city": "Los Angeles",
                "stateOrProvince": "CA",
                "country": "US"
            }
        },
        "name": "Dajian Warehouse - Los Angeles",
        "locationTypes": ["WAREHOUSE"],
        "merchantLocationStatus": "ENABLED"
    }
    
    url = f"https://api.ebay.com/sell/inventory/v1/location/{merchant_location_key}"
    
    print(f"[*] Creating Inventory Location: {merchant_location_key}")
    print(f"[*] URL: {url}")
    print(f"[*] Payload: {json.dumps(location_payload, indent=2)}")
    
    try:
        response = requests.post(url, headers=headers, json=location_payload)
        print(f"\n[*] Response: {response.status_code}")
        
        if response.status_code in [200, 201, 204]:
            print(f"[OK] Location created successfully!")
            print(f"    Merchant Location Key: {merchant_location_key}")
            return merchant_location_key
        elif response.status_code == 409 or (response.status_code == 400 and "already exists" in response.text):
            print(f"[OK] Location already exists, will use it")
            print(f"    Merchant Location Key: {merchant_location_key}")
            return merchant_location_key
        else:
            print(f"[ER] Error: {response.status_code}")
            try:
                print(f"    {response.text[:500]}")
            except:
                print(f"    (Error details encoding issue)")
            
    except Exception as e:
        print(f"[EX] Exception: {str(e)[:100]}")
        return None

def publish_offer_with_location(merchant_location_key):
    """使用有效的 Location 来发布 offer"""
    
    auth = EbayOAuthService("PRODUCTION")
    token = auth.get_valid_token()
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US"
    }
    
    # 使用现有的 CLEANNEW001 SKU
    sku = "CLEANNEW001"
    
    # 先更新 offer，添加 merchantLocationKey
    offer_payload = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": "FIXED_PRICE",
        "availableQuantity": 5,
        "categoryId": "15687",  # 通用电子产品类别
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
        "merchantLocationKey": merchant_location_key  # <-- 关键！添加位置
    }
    
    # 首先获取任何现有 offer
    get_offers_url = "https://api.ebay.com/sell/inventory/v1/offer"
    get_response = requests.get(get_offers_url, headers=headers)
    
    if get_response.status_code != 200:
        print(f"[!] Failed to get offers: {get_response.status_code}")
        return
    
    offers = get_response.json().get("offers", [])
    
    # 找到对应这个 SKU 的 offer
    target_offer_id = None
    for offer in offers:
        if offer.get("sku") == sku:
            target_offer_id = offer.get("offerId")
            break
    
    if not target_offer_id:
        print(f"[!] No offer found for SKU: {sku}")
        print(f"[*] Creating new offer...")
        
        # 创建新 offer
        create_url = "https://api.ebay.com/sell/inventory/v1/offer"
        create_response = requests.post(create_url, headers=headers, json=offer_payload)
        
        if create_response.status_code == 201:
            create_data = create_response.json()
            target_offer_id = create_data.get("offerId")
            print(f"[OK] Offer created: {target_offer_id}")
        else:
            print(f"[ER] Failed to create offer: {create_response.status_code}")
            print(f"    {create_response.text[:500]}")
            return
    else:
        print(f"[*] Found existing offer: {target_offer_id}")
        
        # 更新 offer 添加 location key
        update_url = f"https://api.ebay.com/sell/inventory/v1/offer/{target_offer_id}"
        update_response = requests.post(update_url, headers=headers, json=offer_payload)
        
        if update_response.status_code not in [200, 204]:
            print(f"[ER] Failed to update offer: {update_response.status_code}")
            print(f"    {update_response.text[:500]}")
    
    # 现在发布 offer
    print(f"\n[*] Publishing offer: {target_offer_id}")
    publish_url = f"https://api.ebay.com/sell/inventory/v1/offer/{target_offer_id}/publish"
    
    publish_response = requests.post(publish_url, headers=headers)
    
    print(f"[*] Publish response: {publish_response.status_code}")
    
    if publish_response.status_code == 200:
        data = publish_response.json()
        listing_id = data.get("listingId")
        print(f"[SUCCESS] SUCCESS! Offer published!")
        print(f"           Listing ID: {listing_id}")
        print(f"           View at: https://www.ebay.com/itm/{listing_id}")
        return listing_id
    else:
        print(f"[ER] Publish failed: {publish_response.status_code}")
        print(f"     {publish_response.text[:800]}")
        return None

if __name__ == "__main__":
    print("[*] Starting location creation and publish workflow...\n")
    
    # Step 1: Create location
    location_key = create_inventory_location()
    
    if location_key:
        print("\n" + "="*70)
        # Step 2: Publish offer using the location
        publish_offer_with_location(location_key)
    else:
        print("[ER] Failed to create location")
