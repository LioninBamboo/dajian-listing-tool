import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))
from src.services.ebay_auth import EbayOAuthService

def test_token():
    print("Testing Token Access...")
    try:
        service = EbayOAuthService("PRODUCTION")
        token = service.get_valid_token()
        print(f"✅ Success! Token found: {token[:20]}...")
        return True
    except Exception as e:
        print(f"❌ Failed: {e}")
        return False

if __name__ == "__main__":
    test_token()
