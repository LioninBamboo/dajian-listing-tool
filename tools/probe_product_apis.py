"""测试产品详情、价格和库存API"""
import requests, time, hmac, hashlib, base64, json, os, random, string, urllib3
urllib3.disable_warnings()
from dotenv import load_dotenv
load_dotenv()

client_id = os.getenv('DAJIAN_API_KEY')
client_secret = os.getenv('DAJIAN_API_SECRET')
proxy = os.getenv('DAJIAN_PROXY_URL')
proxies = {'http': proxy, 'https': proxy} if proxy else None
base = 'https://openapi.gigab2b.com'

def make_request(path, payload):
    timestamp = str(int(time.time() * 1000))
    nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
    msg = f'{client_id}&{path}&{timestamp}&{nonce}'
    key = f'{client_id}&{client_secret}&{nonce}'
    sign = base64.b64encode(hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest().encode()).decode()
    
    headers = {'Content-Type': 'application/json', 'client-id': client_id, 'timestamp': timestamp, 'nonce': nonce, 'sign': sign}
    r = requests.post(f'{base}{path}', headers=headers, json=payload, timeout=15, proxies=proxies, verify=False)
    return r.json()

# 测试SKU
test_sku = 'N710P401337K'

# 1. 产品详情查询
print('=== 产品详情查询 ===')
path1 = '/b2b-overseas-api/v1/buyer/product/detailInfo/v1'
result1 = make_request(path1, {'skus': [test_sku]})
print(f"Success: {result1.get('success')}")
print(f"Code: {result1.get('code')}")
if result1.get('success') and result1.get('data'):
    item = result1['data'][0] if isinstance(result1['data'], list) else result1['data']
    print(f"SKU: {item.get('sku')}")
    print(f"Name: {str(item.get('productName', 'N/A'))[:50]}...")
    print(f"Category: {item.get('category')}")
    print(f"Weight: {item.get('weight')} {item.get('weightUnit')}")
    print(f"Dimensions: {item.get('length')}x{item.get('width')}x{item.get('height')} {item.get('lengthUnit')}")
else:
    print(f"Error: {result1.get('msg')} - {result1.get('subMsg')}")

# 2. 产品价格查询
print('\n=== 产品价格查询 ===')
path2 = '/b2b-overseas-api/v1/buyer/product/price/v1'
result2 = make_request(path2, {'skus': [test_sku]})
print(f"Success: {result2.get('success')}")
print(f"Code: {result2.get('code')}")
if result2.get('success') and result2.get('data'):
    item = result2['data'][0] if isinstance(result2['data'], list) else result2['data']
    print(f"SKU: {item.get('sku')}")
    print(f"Price: {item.get('price')} {item.get('currency')}")
    print(f"Shipping: {item.get('shippingFee')}")
    print(f"Available: {item.get('skuAvailable')}")
else:
    print(f"Error: {result2.get('msg')} - {result2.get('subMsg')}")

# 3. 库存查询
print('\n=== 库存查询 ===')
path3 = '/b2b-overseas-api/v1/buyer/inventory/quantity/v2'
result3 = make_request(path3, {'skus': [test_sku]})
print(f"Success: {result3.get('success')}")
print(f"Code: {result3.get('code')}")
if result3.get('success') and result3.get('data'):
    item = result3['data'][0] if isinstance(result3['data'], list) else result3['data']
    print(f"SKU: {item.get('sku')}")
    buyer_info = item.get('buyerInventoryInfo', {})
    seller_info = item.get('sellerInventoryInfo', {})
    print(f"Buyer Available: {buyer_info.get('totalBuyerAvailableInventory')}")
    print(f"Seller Available: {seller_info.get('sellerAvailableInventory')}")
else:
    print(f"Error: {result3.get('msg')} - {result3.get('subMsg')}")

print('\n=== 测试完成 ===')
