"""
测试大建 (GigaCloud) Open API 2.0 连接

验证 API 连通性、签名算法和基本功能
"""
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import requests
import time
import hmac
import hashlib
import base64
import json
import random
import string
import urllib3

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def generate_nonce(length: int = 10) -> str:
    """生成 10 位随机字符"""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


def generate_signature(client_id: str, client_secret: str, api_path: str, 
                      timestamp: str, nonce: str) -> str:
    """
    生成 HMAC-SHA256 签名 (官方算法)
    
    签名规则:
    1. msg = clientId + "&" + apiPath + "&" + timestamp + "&" + nonce
    2. key = clientId + "&" + clientSecret + "&" + nonce
    3. sign = base64(hmac_sha256_hex(msg, key))
    """
    msg = f"{client_id}&{api_path}&{timestamp}&{nonce}"
    key = f"{client_id}&{client_secret}&{nonce}"
    
    hmac_hex = hmac.new(key.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
    signature = base64.b64encode(hmac_hex.encode()).decode()
    return signature


def test_product_list(client_id: str, client_secret: str, base_url: str, proxies: dict = None):
    """测试产品列表查询 API"""
    path = "/b2b-overseas-api/v1/buyer/product/skus/v1"
    url = f"{base_url}{path}"
    
    timestamp = str(int(time.time() * 1000))
    nonce = generate_nonce()
    sign = generate_signature(client_id, client_secret, path, timestamp, nonce)
    
    headers = {
        "Content-Type": "application/json",
        "client-id": client_id,
        "timestamp": timestamp,
        "nonce": nonce,
        "sign": sign
    }
    
    payload = {
        "page": 1,
        "pageSize": 100,
        "sort": 4
    }
    
    print(f"\n📋 测试产品列表查询")
    print(f"   URL: {url}")
    print(f"   Payload: {json.dumps(payload)}")
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15, 
                           verify=False, proxies=proxies)
        data = resp.json()
        
        print(f"   Status: {resp.status_code}")
        print(f"   Success: {data.get('success')}")
        print(f"   Code: {data.get('code')}")
        
        if data.get('success'):
            page_info = data.get('data', {}).get('pageInfo', {})
            records = data.get('data', {}).get('records', [])
            print(f"\n   ✅ 成功!")
            print(f"   📊 分页信息:")
            print(f"      - 当前页: {page_info.get('page')}")
            print(f"      - 总页数: {page_info.get('totalPage')}")
            print(f"      - 每页数: {page_info.get('pageSize')}")
            print(f"      - 总产品: {page_info.get('totalNum')}")
            
            if records:
                print(f"\n   📦 前 3 个产品:")
                for p in records[:3]:
                    sku = p.get('sku', 'N/A')
                    name = p.get('productName', 'N/A')[:40]
                    print(f"      - {sku}: {name}...")
            return True
        else:
            print(f"   ❌ 失败: {data.get('msg')}")
            print(f"      SubMsg: {data.get('subMsg')}")
            return False
            
    except Exception as e:
        print(f"   ❌ 错误: {e}")
        return False


def test_connection(base_url: str, proxies: dict = None):
    """测试基本网络连接"""
    print(f"\n🌐 测试网络连接: {base_url}")
    try:
        resp = requests.get(base_url, timeout=10, verify=False, proxies=proxies)
        print(f"   Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"   ❌ 连接失败: {e}")
        return False


def main():
    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    base_url = os.getenv("DAJIAN_BASE_URL", "https://openapi.gigab2b.com")
    
    # 代理设置
    proxy_url = os.getenv("DAJIAN_PROXY_URL", "").strip()
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    
    print("=" * 60)
    print("🔧 大建 (GigaCloud) Open API 2.0 测试")
    print("=" * 60)
    print(f"Client ID: {client_id[:15]}..." if client_id else "❌ 未配置")
    print(f"Base URL: {base_url}")
    print(f"Proxy: {proxy_url or '未使用'}")
    
    if not client_id or not client_secret:
        print("\n❌ 请配置 DAJIAN_API_KEY 和 DAJIAN_API_SECRET 环境变量")
        return
    
    # 测试网络连接
    if not test_connection(base_url, proxies):
        print("\n⚠️  网络连接失败，请检查代理配置")
        return
    
    # 测试产品列表 API
    if test_product_list(client_id, client_secret, base_url, proxies):
        print("\n" + "=" * 60)
        print("✅ API 测试通过!")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("❌ API 测试失败")
        print("=" * 60)


if __name__ == "__main__":
    main()
