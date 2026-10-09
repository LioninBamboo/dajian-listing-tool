"""测试大建云仓 API 2.0 连接"""
import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.clients.dajian_client import DaJianClient

def test_api_v2():
    load_dotenv()
    
    print("初始化大建云仓 API 2.0 客户端...")
    client = DaJianClient(
        client_id=os.getenv("DAJIAN_API_KEY"),
        client_secret=os.getenv("DAJIAN_API_SECRET"),
        base_url=os.getenv("DAJIAN_BASE_URL", "https://openapi.giga2b.com")
    )
    
    print(f"Base URL: {client.base_url}")
    print("\n尝试获取产品列表...")
    
    try:
        products = client.get_product_list(page=1, page_size=5)
        print(f"✅ 成功获取 {len(products)} 个产品")
        
        if products:
            print("\n第一个产品信息:")
            product = products[0]
            print(f"  SKU: {product.get('sku', 'N/A')}")
            print(f"  名称: {product.get('name', 'N/A')}")
            print(f"  价格: ${product.get('price', 'N/A')}")
            
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_api_v2()
