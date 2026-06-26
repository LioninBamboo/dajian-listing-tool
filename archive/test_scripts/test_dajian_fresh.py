import os
import sys
from dotenv import load_dotenv
import requests
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

def test_dajian_fresh():
    load_dotenv()
    
    api_key = os.getenv("DAJIAN_API_KEY")
    api_secret = os.getenv("DAJIAN_API_SECRET")
    base_url = os.getenv("DAJIAN_BASE_URL", "https://api.gigacloud.com/v1")
    
    print("=" * 60)
    print("大建云仓连接测试")
    print("=" * 60)
    print(f"API Key: {api_key[:8]}..." if api_key else "未设置")
    print(f"Base URL: {base_url}")
    print()
    
    # 步骤1: 获取Token
    print("[1/3] 正在获取访问令牌...")
    token_url = f"{base_url}/oauth/token"
    
    try:
        response = requests.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": api_key,
                "client_secret": api_secret
            },
            timeout=30,
            verify=False  # 禁用SSL验证
        )
        
        print(f"    状态码: {response.status_code}")
        
        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get("access_token")
            print(f"    ✅ 成功获取Token: {access_token[:20]}...")
            
            # 步骤2: 获取产品列表
            print("\n[2/3] 正在获取产品列表...")
            products_url = f"{base_url}/products"
            
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json"
            }
            
            products_response = requests.get(
                products_url,
                headers=headers,
                params={"page": 1, "page_size": 5},
                timeout=30,
                verify=False
            )
            
            print(f"    状态码: {products_response.status_code}")
            
            if products_response.status_code == 200:
                products_data = products_response.json()
                products = products_data.get("data", [])
                print(f"    ✅ 成功获取 {len(products)} 个产品")
                
                if products:
                    print("\n[3/3] 产品示例:")
                    first_product = products[0]
                    print(f"    SKU: {first_product.get('sku')}")
                    print(f"    标题: {first_product.get('title', 'N/A')[:50]}...")
                    print(f"    价格: ${first_product.get('price', 0)}")
                    print(f"    库存: {first_product.get('stock', 0)}")
                else:
                    print("    ⚠️ 产品列表为空")
                    
                print("\n" + "=" * 60)
                print("✅ 大建云仓连接成功！")
                print("=" * 60)
                return True
            else:
                print(f"    ❌ 获取产品失败: {products_response.text[:200]}")
                
        else:
            print(f"    ❌ 获取Token失败: {response.text[:200]}")
            
    except requests.exceptions.SSLError as e:
        print(f"    ❌ SSL错误: {str(e)[:100]}")
        print("    提示: 这通常是网络代理或防火墙问题")
    except requests.exceptions.ConnectionError as e:
        print(f"    ❌ 连接错误: {str(e)[:100]}")
        print("    提示: 无法连接到服务器")
    except requests.exceptions.Timeout as e:
        print(f"    ❌ 超时错误: {str(e)[:100]}")
        print("    提示: 服务器响应超时")
    except Exception as e:
        print(f"    ❌ 未知错误: {str(e)[:100]}")
        
    print("\n" + "=" * 60)
    print("❌ 大建云仓连接失败")
    print("=" * 60)
    return False

if __name__ == "__main__":
    test_dajian_fresh()
