"""
验证 eBay OAuth 授权状态

在云服务器上运行此脚本来检查 eBay Token 是否有效
"""

import os
from dotenv import load_dotenv
from src.services.ebay_auth import EbayOAuthService

# 加载环境变量
load_dotenv()

def verify_authorization():
    """验证 eBay 授权状态"""
    print("🔍 检查 eBay OAuth 授权状态...\n")
    
    # 获取环境配置
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    redirect_uri = os.getenv("EBAY_REDIRECT_URI", "")
    
    print(f"环境: {environment}")
    print(f"Redirect URI: {redirect_uri}\n")
    
    # 创建 OAuth 服务
    oauth = EbayOAuthService(environment)
    
    # 检查授权状态
    if oauth.is_authorized():
        print("✅ eBay 授权成功！\n")
        
        # 获取 token
        try:
            token = oauth.get_valid_token()
            print(f"Access Token (前 50 字符): {token[:50]}...")
            print(f"Token 长度: {len(token)} 字符\n")
            
            # 测试 API 调用
            print("🧪 测试 eBay API 调用...")
            import requests
            
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }
            
            # 测试获取 inventory items
            if environment == "PRODUCTION":
                url = "https://api.ebay.com/sell/inventory/v1/inventory_item"
            else:
                url = "https://api.sandbox.ebay.com/sell/inventory/v1/inventory_item"
            
            response = requests.get(url, headers=headers, params={"limit": 1})
            
            if response.status_code == 200:
                print("✅ API 调用成功！")
                print(f"响应状态码: {response.status_code}")
            else:
                print(f"⚠️ API 调用返回状态码: {response.status_code}")
                print(f"响应: {response.text[:200]}")
            
        except Exception as e:
            print(f"❌ 获取 token 失败: {e}")
    else:
        print("❌ eBay 未授权\n")
        print("请访问以下 URL 完成授权:")
        print(f"{redirect_uri}")
        print("\n点击 '🔐 Authorize with eBay' 按钮")

if __name__ == "__main__":
    verify_authorization()
