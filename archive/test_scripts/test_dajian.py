import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.clients.dajian_client import DaJianClient

def test_dajian():
    load_dotenv()
    
    api_key = os.getenv("DAJIAN_API_KEY")
    api_secret = os.getenv("DAJIAN_API_SECRET")
    base_url = os.getenv("DAJIAN_BASE_URL", "https://api.gigacloud.com/v1")
    
    print(f"Testing Dajian connection...")
    print(f"URL: {base_url}")
    # print(f"Key: {api_key[:4]}...")
    
    client = DaJianClient(api_key, api_secret, base_url)
    
    try:
        # 1. Test Auth
        print("1. Authenticating...")
        token = client.authenticate()
        print(f"✅ Auth Success! Token length: {len(token)}")
        
        # 2. Test Get Products
        print("2. Fetching Products...")
        products = client.get_product_list(page=1, page_size=5)
        print(f"✅ Products Fetched: {len(products)}")
        
        if products:
            print("First Product SKU:", products[0].get('sku'))
        else:
            print("⚠️ List is empty. Response might be OK but no data?")
            
    except Exception as e:
        print(f"\n❌ Error: {e}")

if __name__ == "__main__":
    test_dajian()
