"""Debug and fix the Green Sofa issue"""
import sqlite3
import json
from src.services.ebay_auth import EbayOAuthService
from src.clients.real_ebay_client import create_ebay_session

# 初始化
oauth = EbayOAuthService("PRODUCTION")
token = oauth.get_valid_token()
session = create_ebay_session()

print("=" * 60)
print("1. 检查数据库中的产品状态")
print("=" * 60)

conn = sqlite3.connect('ebay_collection.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("SELECT * FROM collected_products WHERE sku = 'W487P411613-GreenFoam3Seat'")
row = cursor.fetchone()
if row:
    print(f"SKU: {row['sku']}")
    print(f"Status: {row['status']}")
    print(f"Listing ID: {row['listing_id']}")
    print(f"Price: {row['suggested_price']}")
conn.close()

print("\n" + "=" * 60)
print("2. 检查 eBay 上的 Listing 状态")
print("=" * 60)

# 检查 listing 366123110969
listing_id = "366123110969"
url = f"https://api.ebay.com/sell/inventory/v1/offer"
headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json"
}

# 获取所有 offers
response = session.get(url, headers=headers, params={"limit": 100})
print(f"Get offers status: {response.status_code}")

if response.status_code == 200:
    data = response.json()
    offers = data.get("offers", [])
    print(f"Total offers: {len(offers)}")
    
    for offer in offers:
        sku = offer.get("sku", "")
        if "Green" in sku or "W487P411613" in sku:
            print(f"\n Found Green Sofa Offer:")
            print(f"  SKU: {sku}")
            print(f"  Offer ID: {offer.get('offerId')}")
            print(f"  Status: {offer.get('status')}")
            print(f"  Category ID: {offer.get('categoryId', 'NOT SET')}")
            print(f"  Listing ID: {offer.get('listing', {}).get('listingId', 'N/A')}")

print("\n" + "=" * 60)
print("3. 检查特定 SKU 的 offer")
print("=" * 60)

sku = "W487P411613-GreenFoam3Seat"
url = f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}"
response = session.get(url, headers=headers)
print(f"Get offer for SKU status: {response.status_code}")
if response.status_code == 200:
    data = response.json()
    offers = data.get("offers", [])
    for offer in offers:
        print(f"  Offer ID: {offer.get('offerId')}")
        print(f"  Status: {offer.get('status')}")
        print(f"  Category ID: {offer.get('categoryId', 'NOT SET')}")
        
        listing = offer.get("listing", {})
        if listing:
            print(f"  Listing ID: {listing.get('listingId')}")
            print(f"  Listing Status: {listing.get('listingStatus')}")
else:
    print(f"Error: {response.text[:200]}")

print("\n" + "=" * 60)
print("DIAGNOSIS COMPLETE")
print("=" * 60)
