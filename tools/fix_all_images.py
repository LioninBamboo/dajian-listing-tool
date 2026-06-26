"""
批量修复已发布产品的图片 URL — 仅在 eBay live 图片数明显少于本地源图时
触发，并通过 EPS 上传后再 PUT，避免把 GigaB2B 签名 URL 直接写入
inventory_item.product.imageUrls（会被 eBay 拒收，最终只剩 1 张图）。

运行前请确保 eBay 已授权
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.ebay_auth import EbayOAuthService
from src.clients.real_ebay_client import create_real_ebay_client
import requests, json, sqlite3, warnings
import os
from dotenv import load_dotenv

warnings.filterwarnings('ignore')
load_dotenv()

def fix_product_images(sku: str, oauth: EbayOAuthService, ebay_client) -> bool:
    """Fix images for a single product"""
    token = oauth.get_valid_token()
    
    # Get images from DB
    conn = sqlite3.connect('ebay_collection.db')
    cur = conn.cursor()
    cur.execute('SELECT images FROM collected_products WHERE sku=?', (sku,))
    row = cur.fetchone()
    conn.close()
    
    if not row or not row[0]:
        print(f'  [SKIP] No images in DB for {sku}')
        return False
    
    images = json.loads(row[0])
    expected_count = min(len(images), 24)

    # Get current inventory item from eBay
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Content-Language': 'en-US'
    }
    url = f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
    resp = requests.get(url, headers=headers, verify=False, timeout=30)
    
    if resp.status_code != 200:
        print(f'  [ERROR] Failed to get inventory item: {resp.status_code}')
        return False
    
    item = resp.json()
    current_images = (item.get('product') or {}).get('imageUrls') or []

    # Only act when live count is clearly degraded vs local source images.
    if len(current_images) >= max(1, expected_count - 1):
        print(f'  [OK] Images already present for {sku} ({len(current_images)} live, {expected_count} expected)')
        return True

    # Upload to EPS so eBay hosts permanent copies; never PUT raw GigaB2B URLs.
    eps_urls = ebay_client.upload_images_to_eps(images, max_images=24)
    if not eps_urls:
        print(f'  [ERROR] EPS upload returned 0 images for {sku}')
        return False
    
    item.setdefault('product', {})
    item['product']['imageUrls'] = eps_urls
    
    resp = requests.put(url, headers=headers, json=item, verify=False, timeout=30)
    if resp.status_code in [200, 204]:
        print(f'  [FIXED] {sku}: {len(current_images)} -> {len(eps_urls)} images (via EPS)')
        return True
    else:
        print(f'  [ERROR] Failed to update: {resp.status_code} - {resp.text[:200]}')
        return False


def main():
    print('=== 批量修复已发布产品图片 ===')
    print()
    
    env = os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION')
    oauth = EbayOAuthService(env)
    
    if not oauth.is_authorized():
        print('[ERROR] eBay 未授权! 请先完成授权。')
        return
    ebay_client = create_real_ebay_client(env)
    
    # Get all published products
    conn = sqlite3.connect('ebay_collection.db')
    cur = conn.cursor()
    cur.execute('SELECT sku FROM collected_products WHERE status=?', ('PUBLISHED',))
    skus = [row[0] for row in cur.fetchall()]
    conn.close()
    
    print(f'找到 {len(skus)} 个已发布产品')
    print()
    
    fixed = 0
    for sku in skus:
        print(f'处理: {sku}')
        if fix_product_images(sku, oauth, ebay_client):
            fixed += 1
    
    print()
    print(f'完成! 修复了 {fixed}/{len(skus)} 个产品')


if __name__ == '__main__':
    main()

