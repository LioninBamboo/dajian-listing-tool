import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.clients.dajian_client import DaJianClient

def test_real_connection():
    load_dotenv()
    
    # 强制不使用 Mock
    if "USE_MOCK_DAJIAN" in os.environ:
        del os.environ["USE_MOCK_DAJIAN"]
        
    print("Initializing Real DajianClient (Open API 2.0)...")
    # Open API 2.0 Default URL
    base_url = os.getenv("DAJIAN_BASE_URL", "https://open-api.gigacloud.com")
    
    client = DaJianClient(
        api_key=os.getenv("DAJIAN_API_KEY"),
        api_secret=os.getenv("DAJIAN_API_SECRET"),
        base_url=base_url
    )
    
    print(f"Target URL: {client.base_url}")
    # Open API 2.0 does not need explicit authentication step (Signature is per-request)
    # print("Attempting to authenticate...")
    # token = client.authenticate()
    
    print("Fetching products (Testing endpoints /product/page or /product/list)...")
    try:
        products = client.get_product_list(page=1, page_size=1)
        print(f"✅ Success! Fetched {len(products)} products.")
        if products:
            print(f"Sample SKU: {products[0].get('sku')}")
            
    except Exception as e:
        print(f"❌ Connection Failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_real_connection()
