"""
测试视频上传到 eBay
"""
import requests
from src.services.ebay_auth import EbayOAuthService

oauth = EbayOAuthService('PRODUCTION')
token = oauth.get_valid_token()

headers = {
    'Authorization': f'Bearer {token}',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'
}

# 视频 URL
video_url = "https://b2bfiles1.gigab2b.cn/image/wkseller/100244/video_trans/6b3f4daf22174e5be9dd61c27f679ac0.mp4"
title = "71 Inch Furniture Style Dog Crate"

print("="*60)
print("Testing eBay Video Upload")
print("="*60)

# 1. Create video
url = 'https://api.ebay.com/sell/media/v1/video'
payload = {
    "title": title,
    "description": title
}

print(f"\n1. Creating video metadata...")
r = requests.post(url, headers=headers, json=payload)
print(f"Status: {r.status_code}")
print(f"Response: {r.text[:500]}")

if r.status_code == 201:
    # 获取 video_id 从 Location header
    location = r.headers.get('Location', '')
    video_id = location.split('/')[-1] if location else None
    print(f"\n✅ Video created!")
    print(f"Video ID: {video_id}")
    print(f"Location: {location}")
    
    # 2. 获取上传 URL
    if video_id:
        print(f"\n2. Getting upload URL...")
        upload_url = f'https://api.ebay.com/sell/media/v1/video/{video_id}/upload'
        r = requests.get(upload_url, headers=headers)
        print(f"Status: {r.status_code}")
        print(f"Response: {r.text[:500]}")
        
        # 3. 下载视频并上传
        if r.status_code == 200:
            upload_info = r.json()
            print(f"\nUpload URL: {upload_info.get('uploadUrl', 'N/A')[:100]}...")
elif r.status_code == 403:
    print("\n❌ 403 Forbidden - Video API may require additional permissions")
    print("eBay Video API 可能需要额外的 OAuth scope 或账户设置")
else:
    print(f"\n❌ Failed: {r.status_code}")
    try:
        err = r.json()
        print(f"Error: {err}")
    except:
        pass
