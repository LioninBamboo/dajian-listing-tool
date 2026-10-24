"""验证 eBay Inventory Item 中的视频"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_video_uploader import create_session_with_retry
import sqlite3
import json

# Get all products with listing_id
conn = sqlite3.connect('ebay_collection.db')
cursor = conn.cursor()
cursor.execute('''
    SELECT sku, listing_id, optimization 
    FROM collected_products 
    WHERE listing_id IS NOT NULL
''')

products = []
for row in cursor.fetchall():
    opt = json.loads(row[2]) if row[2] else {}
    products.append({
        'sku': row[0],
        'listing_id': row[1],
        'video_id': opt.get('video_id')
    })
conn.close()

print("=" * 60)
print("验证 eBay Inventory Item 视频状态")
print("=" * 60)

oauth = EbayOAuthService('PRODUCTION')
if not oauth.is_authorized():
    print("[Error] Not authorized")
    exit(1)

session = create_session_with_retry(retries=3)
token = oauth.get_valid_token()

for p in products:
    sku = p['sku']
    listing_id = p['listing_id']
    local_video_id = p['video_id']
    
    print(f"\n[{sku}] Listing: {listing_id}")
    print(f"  Local video_id: {local_video_id[:20] if local_video_id else 'None'}...")
    
    # Get inventory item from eBay
    get_url = f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    
    response = session.get(get_url, headers=headers, timeout=60)
    
    if response.status_code == 200:
        item_data = response.json()
        product = item_data.get('product', {})
        video_ids = product.get('videoIds', [])
        
        if video_ids:
            print(f"  ✅ eBay videoIds: {video_ids}")
        else:
            print(f"  ❌ No videoIds in eBay inventory item")
    else:
        print(f"  ⚠️ Could not get inventory item: {response.status_code}")

print("\n" + "=" * 60)
print("完成!")
print("=" * 60)
