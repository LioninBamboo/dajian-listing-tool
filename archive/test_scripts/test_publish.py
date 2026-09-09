"""
手动测试发布API

直接调用发布endpoint测试
"""

import requests
import json

SKU = "TEST-12345"  # 刚刚采集的测试产品

print("=" * 70)
print(f"[TEST] Testing publish for SKU: {SKU}")
print("=" * 70)

url = f"http://localhost:8000/api/publish/{SKU}"

print(f"\n[INFO] Sending POST request to: {url}")

try:
    response = requests.post(url, timeout=60)
    
    print(f"\n[INFO] Response status: {response.status_code}")
    print(f"[INFO] Response headers: {dict(response.headers)}")
    
    # 尝试解析JSON
    try:
        data = response.json()
        print(f"\n[INFO] Response content:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        
        if response.status_code == 200:
            print(f"\n[SUCCESS] Publish succeeded!")
            if 'listing_id' in data:
                print(f"   Listing ID: {data['listing_id']}")
        else:
            print(f"\n[ERROR] Publish failed! Status: {response.status_code}")
            print(f"[ERROR] Response: {json.dumps(data, indent=2, ensure_ascii=False)}")
            
    except json.JSONDecodeError:
        print(f"\n[ERROR] JSON decode failed!")
        print(f"原始响应: {response.text[:1000]}")
        
except requests.exceptions.Timeout:
    print("\n[ERROR] Request timeout (60s)")
    print("服务器可能正在处理,请稍后检查产品状态")
    
except requests.exceptions.ConnectionError:
    print("\n[ERROR] Connection failed! Server may not be running")
    
except Exception as e:
    print(f"\n[ERROR] Error: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
print("\n[INFO] Check server terminal logs for details")
