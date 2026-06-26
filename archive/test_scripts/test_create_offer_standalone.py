import sys
import os
import logging
from dotenv import load_dotenv

# Ensure we can import from src
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.clients.real_ebay_client import create_real_ebay_client

# Configure logging to console
logging.basicConfig(level=logging.INFO)

def test_direct_offer():
    load_dotenv()
    
    print("Initializing eBay Client...")
    client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    
    sku = "TEST-12345"
    price = 199.99
    
    print(f"Testing create_offer for SKU: {sku}...")
    
    try:
        # We don't have category ID easily, let's try without it first or hardcode a known one for furniture
        # 3197 is 'Chairs', widely used.
        response = client.create_offer(sku, price, category_id="3197")
        print("SUCCESS:")
        print(response)
        
    except Exception as e:
        print("\n[!] FATAL ERROR CAUGHT:")
        print(e)
        if hasattr(e, 'response') and e.response is not None:
             print("[ERROR BODY]:")
             print(e.response.text)
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_direct_offer()
