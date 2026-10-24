#!/usr/bin/env python3
"""完整的视频上传流程测试"""
import os
import sys
import requests
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src.services.ebay_auth import EbayOAuthService

oauth = EbayOAuthService()
token = oauth.get_valid_token()

# 目标产品
sku = "N707S003015B"
video_url = "https://b2bfiles1.gigab2b.cn/image/wkseller/8457/video_trans/0e7fecbee9a3ed8c6ef76e1b04de38dd.mp4"
title = "48 Inch Kitchen Island with 2 Bar Stools"

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

print("=" * 60)
print("完整视频上传流程")
print("=" * 60)

# 1. 获取视频信息
print("\n1. 获取视频信息...")
resp = requests.head(video_url, timeout=30, allow_redirects=True)
file_size = int(resp.headers.get('content-length', 0))
content_type = resp.headers.get('content-type', 'video/mp4')
print(f"   Size: {file_size} bytes ({file_size/1024/1024:.2f} MB)")
print(f"   Type: {content_type}")

# 2. 创建 eBay 视频资源
print("\n2. 创建 eBay 视频资源...")
create_url = "https://apim.ebay.com/commerce/media/v1_beta/video"
payload = {
    "title": title[:160],
    "description": f"Product video for {sku}",
    "size": file_size,
    "classification": ["ITEM"]
}

resp = requests.post(create_url, headers=headers, json=payload, timeout=30)
print(f"   Status: {resp.status_code}")

if resp.status_code != 201:
    print(f"   Error: {resp.text}")
    exit(1)

location = resp.headers.get('Location', '')
video_id = location.split('/')[-1] if location else None
print(f"   ✅ Video ID: {video_id}")

# 3. 下载视频
print("\n3. 下载视频...")
video_resp = requests.get(video_url, timeout=120)
video_data = video_resp.content
print(f"   Downloaded: {len(video_data)} bytes")

# 4. 上传视频内容（分块上传）
print("\n4. 上传视频到 eBay...")
upload_url = f"https://apim.ebay.com/commerce/media/v1_beta/video/{video_id}/upload"

# eBay 要求分块上传，每块最大 5MB
chunk_size = 5 * 1024 * 1024  # 5MB
total_size = len(video_data)
offset = 0
part = 1

while offset < total_size:
    end = min(offset + chunk_size, total_size)
    chunk = video_data[offset:end]
    
    content_range = f"bytes {offset}-{end-1}/{total_size}"
    
    upload_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "Content-Length": str(len(chunk)),
        "Content-Range": content_range
    }
    
    print(f"   Uploading part {part}: {content_range}")
    
    resp = requests.post(upload_url, headers=upload_headers, data=chunk, timeout=120)
    print(f"   Response: {resp.status_code}")
    
    if resp.status_code not in [200, 201, 202, 204]:
        print(f"   Error: {resp.text}")
        break
    
    offset = end
    part += 1

print("   ✅ 上传完成!")

# 5. 检查视频状态
print("\n5. 检查视频状态...")
status_url = f"https://apim.ebay.com/commerce/media/v1_beta/video/{video_id}"
resp = requests.get(status_url, headers=headers, timeout=30)
print(f"   Status: {resp.status_code}")
if resp.status_code == 200:
    data = resp.json()
    print(f"   Video Status: {data.get('status')}")
    print(f"   Title: {data.get('title')}")
else:
    print(f"   Response: {resp.text[:200]}")

# 6. 将视频添加到 inventory item
print("\n6. 将视频添加到 Inventory Item...")
inventory_url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"

# 先获取当前 inventory item
get_resp = requests.get(inventory_url, headers=headers, timeout=30)
if get_resp.status_code == 200:
    inv_data = get_resp.json()
    
    # 添加视频 ID
    if "product" not in inv_data:
        inv_data["product"] = {}
    inv_data["product"]["videoIds"] = [video_id]
    
    # 更新 inventory item
    put_resp = requests.put(inventory_url, headers=headers, json=inv_data, timeout=30)
    print(f"   Update Status: {put_resp.status_code}")
    
    if put_resp.status_code == 204:
        print("   ✅ 视频已关联到产品!")
    else:
        print(f"   Response: {put_resp.text[:300]}")
else:
    print(f"   Error getting inventory: {get_resp.status_code}")

print("\n" + "=" * 60)
print(f"视频 ID: {video_id}")
print(f"SKU: {sku}")
print("视频需要几分钟处理后才能在 eBay 上显示")
print("=" * 60)
