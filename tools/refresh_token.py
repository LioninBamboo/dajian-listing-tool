"""Refresh eBay token"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.ebay_auth import EbayOAuthService
from dotenv import load_dotenv
load_dotenv()

oauth = EbayOAuthService('PRODUCTION')
try:
    token = oauth.refresh_access_token()
    print('Refresh success!')
    print(f'Access token received: {len(token.get("access_token", ""))} chars')
except Exception as e:
    print(f'Refresh failed: {e}')
    print("You may need to re-authorize: http://localhost:8001/ebay/auth")
