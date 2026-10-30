"""
上传所有待上传视频到 eBay

使用改进的视频上传服务，包含：
- 重试机制
- SSL 错误处理
- 进度跟踪
"""
import os
import sys
import sqlite3
import json
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_video_uploader import EbayVideoUploader


def get_products_with_videos():
    """Get all products that have videos but no successful video_id"""
    conn = sqlite3.connect('ebay_collection.db')
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT sku, videos, title, optimization 
        FROM collected_products 
        WHERE videos IS NOT NULL AND videos != '[]'
    ''')
    
    products = []
    for row in cursor.fetchall():
        sku = row[0]
        videos = json.loads(row[1]) if row[1] else []
        title = row[2]
        optimization = json.loads(row[3]) if row[3] else {}
        
        # Check if already has successful video upload
        video_id = optimization.get('video_id')
        video_status = optimization.get('video_status')
        
        # Include if: no video_id, or status is FAILED/None. Unsupported sources
        # are text/page links rather than direct video files and should not retry.
        if videos and video_status != 'UNSUPPORTED_SOURCE' and (not video_id or video_status in [None, 'FAILED', 'TIMEOUT']):
            products.append({
                'sku': sku,
                'video_url': videos[0],
                'title': title or sku,
                'video_id': video_id,
                'video_status': video_status
            })
    
    conn.close()
    return products


def main():
    print("=" * 60)
    print("eBay 视频批量上传工具")
    print("=" * 60)
    
    # Initialize OAuth
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = EbayOAuthService(environment)
    
    if not oauth.is_authorized():
        print("[Error] Not authorized. Please run OAuth flow first.")
        return
    
    print(f"[Auth] Environment: {environment}")
    print(f"[Auth] Token valid: ✅")
    
    # Get products with videos
    products = get_products_with_videos()
    
    if not products:
        print("\n[Info] 没有需要上传的视频")
        return
    
    print(f"\n找到 {len(products)} 个待上传视频:")
    for p in products:
        print(f"  - {p['sku']}: {p['title'][:40]}...")
    
    # Initialize uploader
    uploader = EbayVideoUploader(oauth)
    
    # Upload each video
    success_count = 0
    fail_count = 0
    
    for i, product in enumerate(products, 1):
        print(f"\n" + "=" * 60)
        print(f"[{i}/{len(products)}] 上传视频: {product['sku']}")
        print("=" * 60)
        
        try:
            video_id = uploader.upload_video_sync(
                video_url=product['video_url'],
                sku=product['sku'],
                title=product['title'][:80]
            )
            
            if video_id:
                print(f"✅ 上传成功! Video ID: {video_id}")
                success_count += 1
            else:
                print(f"❌ 上传失败")
                fail_count += 1
                
        except Exception as e:
            print(f"❌ 上传出错: {e}")
            fail_count += 1
    
    # Summary
    print("\n" + "=" * 60)
    print("上传完成!")
    print("=" * 60)
    print(f"  成功: {success_count}")
    print(f"  失败: {fail_count}")
    print(f"  总计: {len(products)}")
    
    if success_count > 0:
        print("\n[提示] 视频需要几分钟处理后才能在 eBay Listing 上显示")


if __name__ == "__main__":
    main()
