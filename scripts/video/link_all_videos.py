"""
自动关联所有已上传视频到 eBay Inventory Item
运行此脚本检查视频状态并关联到 Listing
"""
import os
import sys
import sqlite3
import json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_video_uploader import EbayVideoUploader, create_session_with_retry


def get_videos_to_link():
    """Get all products with video_id that need linking"""
    conn = sqlite3.connect('ebay_collection.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT sku, optimization, listing_id 
        FROM collected_products 
        WHERE optimization LIKE '%video_id%'
        AND listing_id IS NOT NULL
    ''')
    
    products = []
    for row in cursor.fetchall():
        sku = row[0]
        opt = json.loads(row[1]) if row[1] else {}
        listing_id = row[2]
        video_id = opt.get('video_id')
        video_status = opt.get('video_status')
        
        if video_id:
            products.append({
                'sku': sku,
                'video_id': video_id,
                'video_status': video_status,
                'listing_id': listing_id
            })
    
    conn.close()
    return products


def update_video_status_in_db(sku: str, status: str):
    """Update video status in database"""
    conn = sqlite3.connect('ebay_collection.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT optimization FROM collected_products WHERE sku = ?', (sku,))
    row = cursor.fetchone()
    
    if row:
        opt = json.loads(row[0]) if row[0] else {}
        opt['video_status'] = status
        cursor.execute('UPDATE collected_products SET optimization = ? WHERE sku = ?', (json.dumps(opt), sku))
        conn.commit()
    
    conn.close()


def add_video_to_inventory(oauth, sku: str, video_id: str):
    """Add video to eBay inventory item"""
    session = create_session_with_retry(retries=3)
    token = oauth.get_valid_token()
    
    base_url = "https://api.ebay.com"
    get_url = f"{base_url}/sell/inventory/v1/inventory_item/{sku}"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    
    response = session.get(get_url, headers=headers, timeout=60)
    
    if response.status_code != 200:
        return False, f"Could not get inventory item: {response.status_code}"
    
    item_data = response.json()
    product = item_data.get("product", {})
    video_ids = product.get("videoIds", [])
    
    if video_id in video_ids:
        return True, "Already linked"
    
    video_ids.append(video_id)
    product["videoIds"] = video_ids
    item_data["product"] = product
    
    put_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json"
    }
    
    response = session.put(get_url, headers=put_headers, json=item_data, timeout=120)
    
    if response.status_code in [200, 204]:
        return True, "Linked successfully"
    else:
        return False, f"Failed: {response.status_code}"


def main():
    print("=" * 60)
    print("自动关联视频到 eBay Inventory")
    print("=" * 60)
    
    oauth = EbayOAuthService('PRODUCTION')
    
    if not oauth.is_authorized():
        print("[Error] Not authorized")
        return
    
    print("[Auth] ✅ Token valid\n")
    
    uploader = EbayVideoUploader(oauth)
    products = get_videos_to_link()
    
    if not products:
        print("没有需要关联的视频")
        return
    
    print(f"找到 {len(products)} 个有视频的产品\n")
    
    linked = 0
    processing = 0
    failed = 0
    
    for p in products:
        sku = p['sku']
        video_id = p['video_id']
        current_status = p['video_status']
        
        print(f"[{sku}]")
        print(f"  Video ID: {video_id[:25]}...")
        print(f"  Current Status: {current_status}")
        
        # Check video status on eBay
        status = uploader.get_video_status(video_id)
        ebay_status = status.get('status', 'UNKNOWN')
        print(f"  eBay Status: {ebay_status}")
        
        if ebay_status == 'LIVE':
            # Update local status
            if current_status != 'LIVE':
                update_video_status_in_db(sku, 'LIVE')
            
            # Link to inventory
            success, msg = add_video_to_inventory(oauth, sku, video_id)
            if success:
                print(f"  ✅ {msg}")
                linked += 1
            else:
                print(f"  ❌ {msg}")
                failed += 1
                
        elif ebay_status == 'PROCESSING':
            print(f"  ⏳ Still processing...")
            processing += 1
        else:
            print(f"  ⚠️ Unexpected: {ebay_status}")
            failed += 1
        
        print()
    
    print("=" * 60)
    print("完成!")
    print("=" * 60)
    print(f"  已关联: {linked}")
    print(f"  处理中: {processing}")
    print(f"  失败: {failed}")
    
    if processing > 0:
        print(f"\n[提示] {processing} 个视频仍在处理中，请几分钟后再运行此脚本")


if __name__ == "__main__":
    main()
