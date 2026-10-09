"""
Test eBay Publishing - Simple Version
"""

import requests
import json

SKU = "W2339P424643-BlackLHF"

print("=" * 70)
print(f"Testing publish for SKU: {SKU}")
print("=" * 70)

url = f"http://localhost:8000/api/publish/{SKU}"

print(f"\nSending POST to: {url}")

try:
    response = requests.post(url, timeout=60)
    
    print(f"\nStatus Code: {response.status_code}")
    
    try:
        data = response.json()
        print(f"\nResponse:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        
        if response.status_code == 200:
            print(f"\nSUCCESS!")
            if 'listing_id' in data:
                print(f"Listing ID: {data['listing_id']}")
        else:
            print(f"\nFAILED!")
            
    except json.JSONDecodeError:
        print(f"\nJSON parse failed!")
        print(f"Raw response: {response.text[:1000]}")
        
except Exception as e:
    print(f"\nError: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
