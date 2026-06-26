"""Fix specific product images.

Always uploads source images to eBay EPS first; never PUTs raw GigaB2B
signed URLs into `inventory_item.product.imageUrls` (those collapse to a
single live image because eBay can't fetch most signed URLs).
"""
import os
import sys
from pathlib import Path

# Add parent to path first
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.ebay_auth import EbayOAuthService
from src.clients.real_ebay_client import create_real_ebay_client
import requests, json, sqlite3, warnings
warnings.filterwarnings('ignore')

from dotenv import load_dotenv
load_dotenv()

def fix_product(sku):
    env = os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION')
    oauth = EbayOAuthService(env)
    ebay_client = create_real_ebay_client(env)
    token = oauth.get_valid_token()
    
    # Get images from DB
    conn = sqlite3.connect('ebay_collection.db')
    cur = conn.cursor()
    cur.execute('SELECT images FROM collected_products WHERE sku=?', (sku,))
    row = cur.fetchone()
    conn.close()
    
    if not row or not row[0]:
        print(f'{sku}: No images in DB')
        return False
    
    images = json.loads(row[0])
    print(f'{sku}: DB has {len(images)} images')
    
    # Get current inventory item
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Content-Language': 'en-US'
    }
    url = f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
    resp = requests.get(url, headers=headers, verify=False, timeout=30)
    
    if resp.status_code != 200:
        print(f'{sku}: Not found on eBay')
        return False
    
    item = resp.json()
    current_count = len((item.get('product') or {}).get('imageUrls') or [])
    print(f'{sku}: eBay currently has {current_count} images')

    # Upload to EPS and use eBay-hosted URLs (raw GigaB2B URLs are unreliable).
    eps_urls = ebay_client.upload_images_to_eps(images, max_images=24)
    if not eps_urls:
        print(f'{sku}: EPS upload returned 0 images, abort')
        return False
    print(f'{sku}: Uploaded {len(eps_urls)} images to EPS')

    item.setdefault('product', {})
    item['product']['imageUrls'] = eps_urls
    
    # Fix weight if invalid
    if 'packageWeightAndSize' in item:
        weight = item['packageWeightAndSize'].get('weight', {})
        if not weight.get('value') or weight.get('value') == 0:
            print(f'{sku}: Fixing weight...')
            item['packageWeightAndSize']['weight'] = {'value': 50.0, 'unit': 'POUND'}
    
    resp = requests.put(url, headers=headers, json=item, verify=False, timeout=30)
    
    if resp.status_code in [200, 204]:
        print(f'{sku}: SUCCESS - Updated to {len(eps_urls)} images')
        return True
    else:
        print(f'{sku}: FAILED - {resp.status_code}')
        error = resp.json() if resp.text else {}
        if error.get('errors'):
            for e in error['errors'][:2]:
                print(f'  Error: {e.get("message", "Unknown")}')
        return False


if __name__ == '__main__':
    if len(sys.argv) > 1:
        skus = sys.argv[1:]
    else:
        skus = ['W2899P435700', 'W1162P386373', 'W1911P195002']
    for sku in skus:
        print(f'\n=== Fixing {sku} ===')
        fix_product(sku)

