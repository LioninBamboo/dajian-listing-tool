import sys
import os
import urllib.parse
from dotenv import load_dotenv

# Ensure we can import from src
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.services.ebay_auth import EbayOAuthService

def run_manual_auth():
    load_dotenv()
    
    # Initialize Service
    env = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    service = EbayOAuthService(env)
    
    # 1. Show Authorization URL
    print("\n" + "="*60)
    print(" MANUAL EBAY AUTHORIZATION ")
    print("="*60)
    print(f"Environment:  {env}")
    print(f"Redirect URI: {service.redirect_uri}")
    print(f"(Must match 'Your auth accepted URL' in eBay Portal exactly)")
    print("-" * 60)
    
    auth_url = service.get_authorization_url()
    print("\n1. Please copy and open this URL in your browser:\n")
    print(auth_url)
    
    print("\n" + "-" * 60)
    print("2. After you agree, you will be redirected to Google.")
    print("   The address bar will look like:")
    print("   https://www.google.com/?code=v^1.1#i^1...&expires_in=...")
    print("\n3. Copy the ENTIRE URL from your browser address bar and paste it below:")
    print("-" * 60)
    
    full_url = input("\nPaste full URL here > ").strip()
    
    if not full_url:
        print("Error: No URL provided.")
        return

    # 2. Extract Code
    try:
        parsed = urllib.parse.urlparse(full_url)
        query_params = urllib.parse.parse_qs(parsed.query)
        
        code = query_params.get('code', [None])[0]
        
        if not code:
            print("\n[!] Error: Could not find 'code' parameter in the URL.")
            print(f"Parsed query: {query_params.keys()}")
            return
            
        print(f"\n[+] Authorization Code extracted!")
        print(f"    Code length: {len(code)}")
        
    except Exception as e:
        print(f"\n[!] Failed to parse URL: {e}")
        return

    # 3. Exchange for Token
    print("\n4. Exchanging code for Access Token...")
    try:
        token_data = service.exchange_code_for_token(code)
        
        print("\n" + "="*60)
        print(" SUCCESS! ")
        print("="*60)
        print(f"Access Token: {token_data.get('access_token')[:20]}...")
        print(f"Expires In:   {token_data.get('expires_in')} seconds")
        print("Token has been saved to database.")
        print("You can now run debug_publish.py")
        
    except Exception as e:
        print("\n" + "="*60)
        print(" FAILED ")
        print("="*60)
        print(f"Error: {e}")
        print("\nTip: Authentication codes expire very quickly (mins).")
        print("     Make sure your Redirect URL matches .env EXACTLY.")

if __name__ == "__main__":
    run_manual_auth()
