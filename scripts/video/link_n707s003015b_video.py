"""关联 N707S003015B 的视频"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_video_uploader import EbayVideoUploader, create_session_with_retry

oauth = EbayOAuthService('PRODUCTION')
uploader = EbayVideoUploader(oauth)

# Check N707S003015B status
video_id = 'bca6c86419b0a5acd2472853fffff109'
sku = 'N707S003015B'

print(f"Checking video status for {sku}...")
status = uploader.get_video_status(video_id)
ebay_status = status.get('status', 'UNKNOWN')
print(f"Video Status: {ebay_status}")

if ebay_status == 'LIVE':
    print("Video is LIVE! Adding to inventory...")
    
    session = create_session_with_retry(retries=3)
    token = oauth.get_valid_token()
    
    get_url = f'https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    
    response = session.get(get_url, headers=headers, timeout=60)
    if response.status_code == 200:
        item_data = response.json()
        product = item_data.get('product', {})
        video_ids = product.get('videoIds', [])
        
        if video_id not in video_ids:
            video_ids.append(video_id)
            product['videoIds'] = video_ids
            item_data['product'] = product
            
            put_headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Content-Language': 'en-US',
                'Accept': 'application/json'
            }
            
            response = session.put(get_url, headers=put_headers, json=item_data, timeout=120)
            print(f'Update response: {response.status_code}')
            if response.status_code in [200, 204]:
                print('SUCCESS! Video added to N707S003015B')
            else:
                print(f'Error: {response.text[:200]}')
        else:
            print("Video already in inventory item")
    else:
        print(f'Get inventory item failed: {response.status_code}')
else:
    print(f"Video still processing... wait a few minutes and try again")
