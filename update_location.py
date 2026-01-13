"""更新 Inventory Location 到 Los Angeles, CA"""
import requests
from src.services.ebay_auth import EbayOAuthService

auth = EbayOAuthService('PRODUCTION')
token = auth.get_valid_token()

headers = {
    'Authorization': f'Bearer {token}',
    'Content-Type': 'application/json',
    'Content-Language': 'en-US'
}

# 创建新的 Location (Los Angeles)
location_payload = {
    'location': {
        'address': {
            'city': 'Los Angeles',
            'stateOrProvince': 'CA',
            'country': 'US'
        }
    },
    'name': 'Dajian LA Warehouse',
    'locationTypes': ['WAREHOUSE'],
    'merchantLocationStatus': 'ENABLED'
}

# 使用新的 key
new_key = 'DAJIAN_LA_WAREHOUSE'
url = f'https://api.ebay.com/sell/inventory/v1/location/{new_key}'

# Create new location
response = requests.post(url, headers=headers, json=location_payload)
print(f'POST Response: {response.status_code}')

if response.status_code in [200, 201, 204]:
    print(f'[OK] New Location created: {new_key}')
    print('[OK] City: Los Angeles, CA')
elif response.status_code == 409 or 'already exists' in response.text:
    print(f'[OK] Location {new_key} already exists')
else:
    print(f'Response: {response.text[:300]}')
    
# Verify by GET
get_resp = requests.get(url, headers=headers)
if get_resp.status_code == 200:
    data = get_resp.json()
    addr = data.get('location', {}).get('address', {})
    city = addr.get('city')
    state = addr.get('stateOrProvince')
    country = addr.get('country')
    print(f'[INFO] Location verified: {city}, {state}, {country}')
