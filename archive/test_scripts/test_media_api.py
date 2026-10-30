"""
测试 eBay Media API 正确端点
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

video_url = "https://b2bfiles1.gigab2b.cn/image/wkseller/100244/video_trans/6b3f4daf22174e5be9dd61c27f679ac0.mp4"
title = "71 Inch Furniture Style Dog Crate"

print("="*60)
print("Testing Different eBay Media API Endpoints")
print("="*60)

# 尝试不同的端点
endpoints = [
    ('commerce/media/v1/video', 'POST'),
    ('sell/media/v1/video', 'POST'),
    ('commerce/media/v1beta/video', 'POST'),
]

for endpoint, method in endpoints:
    url = f'https://api.ebay.com/{endpoint}'
    print(f"\n📹 Testing: {url}")
    
    payload = {
        "title": title[:80],
        "description": title[:500]
    }
    
    if method == 'POST':
        r = requests.post(url, headers=headers, json=payload)
    else:
        r = requests.get(url, headers=headers)
    
    print(f"   Status: {r.status_code}")
    if r.status_code != 404:
        print(f"   Response: {r.text[:300]}")
    
    if r.status_code == 201:
        print(f"   ✅ Success!")
        location = r.headers.get('Location', '')
        print(f"   Location: {location}")
        break

# 检查账户是否有 Media API 权限
print("\n" + "="*60)
print("Checking Available Scopes")
print("="*60)
print("""
eBay Media API (视频上传) 可能需要:
1. 特殊的 OAuth scopes (例如 https://api.ebay.com/oauth/api_scope/sell.media)
2. 账户需要在 eBay Developer Portal 启用 Media API
3. 可能需要 Production 环境的额外审批

目前的解决方案:
- 视频可以在产品发布后通过 Seller Hub 手动添加
- 或者在产品描述中嵌入视频链接
""")
