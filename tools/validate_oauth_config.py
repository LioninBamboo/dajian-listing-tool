#!/usr/bin/env python3
"""
eBay OAuth Configuration Validator

验证你的 eBay OAuth 配置是否正确。
运行此脚本可快速诊断常见问题。
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load env vars
load_dotenv()

def check(name, value, validator=None):
    """Helper to print check result"""
    if not value:
        print(f"❌ {name}: NOT SET")
        return False
    
    if validator:
        if validator(value):
            print(f"✅ {name}: {value[:50]}..." if len(value) > 50 else f"✅ {name}: {value}")
            return True
        else:
            print(f"⚠️  {name}: {value} (WARNING - see details below)")
            return False
    else:
        print(f"✅ {name}: {value[:50]}..." if len(value) > 50 else f"✅ {name}: {value}")
        return True

def validate_redirect_uri(uri):
    """Check if redirect URI looks reasonable"""
    if uri == "https://www.google.com":
        return False  # This is the broken default
    if uri.startswith("http"):
        return True
    return False

def validate_environment(env):
    """Check if environment is valid"""
    return env in ["PRODUCTION", "SANDBOX"]

def main():
    print("=" * 70)
    print("🔍 eBay OAuth Configuration Validator")
    print("=" * 70)
    
    all_ok = True
    
    print("\n📋 Environment Variables Check:")
    print("-" * 70)
    
    # Check required vars
    app_id = os.getenv("EBAY_APP_ID")
    cert_id = os.getenv("EBAY_CERT_ID")
    dev_id = os.getenv("EBAY_DEV_ID")
    redirect_uri = os.getenv("EBAY_REDIRECT_URI")
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    
    all_ok &= check("EBAY_APP_ID", app_id)
    all_ok &= check("EBAY_CERT_ID", cert_id)
    all_ok &= check("EBAY_DEV_ID", dev_id)
    all_ok &= check("EBAY_REDIRECT_URI", redirect_uri, validate_redirect_uri)
    all_ok &= check("EBAY_ENVIRONMENT", environment, validate_environment)
    
    print("\n🔐 OAuth Configuration Analysis:")
    print("-" * 70)
    
    # Analyze redirect URI
    if redirect_uri:
        if redirect_uri == "https://www.google.com":
            print("⚠️  CRITICAL: EBAY_REDIRECT_URI is still set to https://www.google.com")
            print("   This is the default broken configuration.")
            print("   👉 You MUST update this after deploying to Streamlit Cloud")
            print("")
            print("   Steps:")
            print("   1. Deploy streamlit_app.py to Streamlit Cloud")
            print("   2. Copy your Streamlit app URL (e.g., https://your-app.streamlit.app)")
            print("   3. Update .env: EBAY_REDIRECT_URI=https://your-app.streamlit.app/ebay/callback")
            print("   4. Update eBay Developer Portal with same URL")
            all_ok = False
        elif redirect_uri.startswith("http://localhost"):
            print("ℹ️  Redirect URI is set for local development:")
            print(f"   {redirect_uri}")
            print("   This works for local development but NOT for production.")
            print("   For production, use Streamlit Cloud HTTPS URL.")
        elif redirect_uri.startswith("https://"):
            if "streamlit" in redirect_uri:
                print("✅ Redirect URI looks good (Streamlit Cloud URL)")
            else:
                print(f"✅ Redirect URI is HTTPS: {redirect_uri}")
                print("   Make sure this matches exactly in eBay Developer Portal")
        else:
            print(f"⚠️  Redirect URI doesn't start with http:// or https://: {redirect_uri}")
            all_ok = False
    
    # Check environment vs app ID type
    if app_id:
        if "PRD" in app_id:
            if environment == "PRODUCTION":
                print("✅ Using PRODUCTION credentials with PRODUCTION environment ✓")
            else:
                print("⚠️  WARNING: You have PRODUCTION credentials but EBAY_ENVIRONMENT=SANDBOX")
                print("   This will cause token errors. Set EBAY_ENVIRONMENT=PRODUCTION")
                all_ok = False
        elif "SANDBOX" in app_id.upper():
            if environment == "SANDBOX":
                print("✅ Using SANDBOX credentials with SANDBOX environment ✓")
            else:
                print("⚠️  WARNING: You have SANDBOX credentials but EBAY_ENVIRONMENT=PRODUCTION")
                print("   This will cause token errors. Set EBAY_ENVIRONMENT=SANDBOX")
                all_ok = False
    
    print("\n📂 Token Storage:")
    print("-" * 70)
    
    token_db = Path("ebay_tokens.db")
    if token_db.exists():
        print(f"✅ Token database exists: {token_db}")
        print(f"   Size: {token_db.stat().st_size} bytes")
        print("   (This is good - means you've done OAuth before)")
        
        # Try to check token validity
        try:
            from src.services.ebay_auth import EbayOAuthService
            oauth = EbayOAuthService(environment)
            if oauth.is_authorized():
                print("✅ Token is VALID and not expired")
                print("   You can start using eBay API now!")
            else:
                print("⚠️  Token exists but appears invalid or expired")
                print("   You need to re-authorize")
        except Exception as e:
            print(f"ℹ️  Could not check token status: {e}")
    else:
        print("❌ Token database not found: ebay_tokens.db")
        print("   You haven't done OAuth yet. Run the authorization flow:")
        print("   1. Deploy to Streamlit Cloud")
        print("   2. Visit https://your-app.streamlit.app")
        print("   3. Click '🔐 Authorize with eBay'")
        all_ok = False
    
    print("\n📝 Streamlit App Status:")
    print("-" * 70)
    
    if redirect_uri and "streamlit" in redirect_uri:
        print("✅ Streamlit URL is configured")
        print("   Make sure your app is deployed and accessible")
    else:
        print("ℹ️  Streamlit URL not yet configured")
        print("   Follow these steps:")
        print("   1. Push code to GitHub: git push origin main")
        print("   2. Go to https://streamlit.io/cloud")
        print("   3. Click 'New app' and select your repo")
        print("   4. Set main file to: streamlit_app.py")
        print("   5. Click 'Deploy'")
        print("   6. Copy the HTTPS URL from the browser tab")
        print("   7. Update .env with EBAY_REDIRECT_URI=<your-url>/ebay/callback")
    
    # Summary
    print("\n" + "=" * 70)
    if all_ok:
        print("✅ All checks passed! Your configuration looks good.")
        print("\n🚀 Next steps:")
        print("   1. python debug_token.py  # Check if token is valid")
        print("   2. python test_real_dajian.py  # Test API access")
    else:
        print("❌ Some issues found. Please address the warnings above.")
        print("\n📖 For detailed help, see:")
        print("   - OAUTH_QUICK_START.md")
        print("   - EBAY_AUTH_DIAGNOSIS.md")
        print("   - STREAMLIT_DEPLOY.md")
    
    print("=" * 70)
    
    return 0 if all_ok else 1

if __name__ == "__main__":
    sys.exit(main())
