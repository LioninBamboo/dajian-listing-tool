import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.clients.browser_dajian_client import BrowserDajianClient

def log(msg):
    print(msg)
    with open("browser_test_result.txt", "a", encoding="utf-8") as f:
        f.write(msg + "\n")

def test_browser_client():
    # Clear previous log
    with open("browser_test_result.txt", "w", encoding="utf-8") as f:
        f.write("Starting Browser Test...\n")

    load_dotenv()
    
    log("Initializing Browser DajianClient...")
    client = BrowserDajianClient(
        api_key=os.getenv("DAJIAN_API_KEY"),
        api_secret=os.getenv("DAJIAN_API_SECRET"),
        base_url=os.getenv("DAJIAN_BASE_URL", "https://api.gigacloud.com/v1")
    )
    
    log("Attempting authentication via Browser...")
    try:
        token = client.authenticate()
        log(f"✅ Auth Success! Token: {token[:10]}...")
        
        log("Fetching products via Browser...")
        products = client.get_product_list(page=1, page_size=2)
        log(f"✅ Fetched {len(products)} products.")
        
        if products:
            sku = products[0].get('sku', 'UNKNOWN')
            log(f"Sample SKU: {sku}")
            
            log(f"Fetching details for {sku}...")
            detail = client.get_product_detail(sku)
            log(f"✅ Detail Fetched: {detail.get('title', 'N/A')[:30]}...")
            
    except Exception as e:
        log(f"[ERROR] Browser Test Failed: {repr(e)}")
        import traceback
        with open("browser_test_result.txt", "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)

if __name__ == "__main__":
    test_browser_client()
