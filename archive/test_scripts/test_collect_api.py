"""
测试产品采集API

模拟浏览器插件发送产品数据
"""

import requests
import json

# 测试数据
test_product = {
    "sku": "TEST-12345",
    "title": "Test Product Title",
    "price": 29.99,
    "shipping": 5.99,
    "stock": 10,
    "description": "This is a test product description",
    "images": [
        "https://example.com/image1.jpg",
        "https://example.com/image2.jpg"
    ],
    "videos": [],
    "attributes": {
        "Brand": "Test Brand",
        "Color": "Blue"
    },
    "url": "https://example.com/product/test-12345"
}

print("=" * 70)
print("🧪 测试产品采集API")
print("=" * 70)

url = "http://localhost:8000/api/collect"

print(f"\n📤 发送请求到: {url}")
print(f"📦 产品数据:")
print(json.dumps(test_product, indent=2))

try:
    response = requests.post(url, json=test_product)
    
    print(f"\n📥 响应状态码: {response.status_code}")
    print(f"📥 响应头: {dict(response.headers)}")
    print(f"\n📥 响应内容:")
    
    # 尝试解析JSON
    try:
        data = response.json()
        print(json.dumps(data, indent=2))
        
        if data.get("status") == "success":
            print(f"\n✅ 采集成功! SKU: {data.get('sku')}")
        else:
            print(f"\n❌ 采集失败: {data.get('message')}")
            
    except json.JSONDecodeError as e:
        print(f"❌ JSON解析失败!")
        print(f"原始响应: {response.text[:500]}")
        
except requests.exceptions.ConnectionError:
    print("\n❌ 连接失败! 服务器可能未运行")
    print("请确保运行: python server.py")
    
except Exception as e:
    print(f"\n❌ 错误: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
