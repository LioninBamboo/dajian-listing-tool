"""Test publish API directly"""
import requests
import time

sku = "W5216S00001-WhiteWalnutMDFMetalDog"
print(f"Publishing SKU: {sku}")
print("="*50)

try:
    start = time.time()
    r = requests.post(f'http://localhost:8000/api/publish/{sku}', timeout=180)
    elapsed = time.time() - start
    
    print(f"Time: {elapsed:.1f}s")
    print(f"Status: {r.status_code}")
    print(f"Response: {r.text}")
except Exception as e:
    print(f"Error: {e}")
