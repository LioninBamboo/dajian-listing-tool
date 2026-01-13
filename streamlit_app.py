"""
Streamlit OAuth Callback Handler for eBay Authorization

This app runs on Streamlit Cloud and handles the OAuth callback from eBay.
Streamlit Cloud provides HTTPS URLs which eBay requires.

Deployment Steps:
1. Push this repo to GitHub
2. Go to https://streamlit.io/cloud
3. Click "New app" → select this repo
4. Set main file path to: streamlit_app.py
5. Copy the HTTPS URL from Streamlit Cloud
6. Update .env: EBAY_REDIRECT_URI=https://your-app.streamlit.app/ebay/callback
7. Register that URL in eBay Developer Portal
8. Visit /ebay/auth to start authorization
"""

import streamlit as st
import os
import sys
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables
load_dotenv()

# Add root to path for imports
root_dir = Path(__file__).parent
sys.path.insert(0, str(root_dir))

from src.services.ebay_auth import EbayOAuthService

# ============================================================================
# Page Config
# ============================================================================
st.set_page_config(
    page_title="eBay OAuth Authorization",
    page_icon="🛍️",
    layout="centered",
    initial_sidebar_state="collapsed"
)

st.title("🛍️ eBay OAuth Authorization")

# ============================================================================
# Helper Functions
# ============================================================================

def get_streamlit_url():
    """Get the current Streamlit app URL"""
    # In Streamlit Cloud, this is automatically set
    if "STREAMLIT_SERVER_HEADLESS" in os.environ:
        # Running on Streamlit Cloud
        return "https://your-app-name.streamlit.app"
    else:
        # Local development
        return "http://localhost:8501"

def get_oauth_service():
    """Get eBay OAuth service"""
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    return EbayOAuthService(environment)

# ============================================================================
# Main App Logic
# ============================================================================

# Get query parameters
query_params = st.query_params

# Check if this is an OAuth callback
if "code" in query_params or "ebayktn" in query_params:
    st.subheader("🔐 Processing Authorization...")
    
    # Get the authorization code
    auth_code = query_params.get("code") or query_params.get("ebayktn")
    error = query_params.get("error")
    error_description = query_params.get("error_description")
    
    if error:
        st.error(f"❌ Authorization Failed")
        st.write(f"**Error:** {error}")
        st.write(f"**Description:** {error_description}")
        st.info("Please go back and try again, or contact support.")
    else:
        try:
            st.info(f"🔄 Exchanging authorization code...")
            
            oauth = get_oauth_service()
            
            # Exchange code for token
            token_data = oauth.exchange_code_for_token(auth_code)
            
            st.success("✅ Authorization Successful!")
            st.write("Your eBay account has been authorized. You can now:")
            st.write("- Create and manage product listings")
            st.write("- Upload inventory items")
            st.write("- Publish offers to eBay")
            
            with st.expander("📋 Token Details (for debugging)"):
                st.json({
                    "token_type": token_data.get("token_type"),
                    "expires_in": token_data.get("expires_in"),
                    "access_token": token_data.get("access_token")[:20] + "..." if token_data.get("access_token") else None
                })
            
            st.success("💾 Token saved to `ebay_tokens.db`")
            st.write("You can now use your eBay API client to publish listings.")
            
        except Exception as e:
            st.error(f"❌ Token Exchange Failed")
            st.write(f"**Error:** {str(e)}")
            st.write("Please check:")
            st.write("1. Your EBAY_APP_ID and EBAY_CERT_ID are correct")
            st.write("2. Your EBAY_REDIRECT_URI matches the registered URL in eBay")
            st.write("3. The authorization code is not expired")
            
            with st.expander("🔧 Debug Info"):
                st.write(f"Environment: {os.getenv('EBAY_ENVIRONMENT', 'SANDBOX')}")
                st.write(f"Auth Code: {auth_code[:30]}..." if auth_code else "None")

else:
    # Show authorization start page
    st.subheader("📝 Start Authorization")
    st.write("Click the button below to authorize your eBay account:")
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("🔐 Authorize with eBay", type="primary", use_container_width=True):
            try:
                oauth = get_oauth_service()
                auth_url = oauth.get_authorization_url(state="streamlit_auth")
                
                st.info("ℹ️ Redirecting to eBay...")
                st.write("After authorization, you will be redirected back to this page.")
                
                # Use HTML to redirect (since streamlit doesn't have native redirect)
                st.markdown(f"""
                    <script>
                    window.location.href = "{auth_url}";
                    </script>
                """, unsafe_allow_html=True)
                
            except Exception as e:
                st.error(f"❌ Failed to generate authorization URL: {str(e)}")
                st.write("Check your environment variables:")
                st.write(f"- EBAY_APP_ID: {'✓' if os.getenv('EBAY_APP_ID') else '✗'}")
                st.write(f"- EBAY_CERT_ID: {'✓' if os.getenv('EBAY_CERT_ID') else '✗'}")
                st.write(f"- EBAY_REDIRECT_URI: {'✓' if os.getenv('EBAY_REDIRECT_URI') else '✗'}")
    
    with col2:
        if st.button("ℹ️ Check Status", use_container_width=True):
            try:
                oauth = get_oauth_service()
                if oauth.is_authorized():
                    st.success("✅ Already Authorized")
                    token = oauth.get_valid_token()
                    st.write(f"Access token: {token[:20]}...")
                else:
                    st.warning("⚠️ Not Authorized")
            except Exception as e:
                st.error(f"❌ Error: {str(e)}")
    
    # Info section
    st.divider()
    st.subheader("📌 Setup Instructions")
    
    st.info("""
    **If this is your first time:**
    
    1. Make sure you have registered this URL in eBay Developer Portal:
       - Go to [eBay Developer Portal](https://developer.ebay.com)
       - Edit your app settings
       - Set **Auth Accepted URL** to: `https://your-app-name.streamlit.app/ebay/callback`
    
    2. Verify your environment variables are set correctly:
       - Check your `.env` file has: `EBAY_REDIRECT_URI=https://your-app-name.streamlit.app/ebay/callback`
    
    3. Click **Authorize with eBay** above
    """)
    
    # Status display
    with st.expander("🔍 Diagnostics"):
        st.write("**Current Configuration:**")
        st.code(f"""
EBAY_APP_ID: {os.getenv('EBAY_APP_ID', 'NOT SET')[:30]}...
EBAY_CERT_ID: {os.getenv('EBAY_CERT_ID', 'NOT SET')[:30]}...
EBAY_REDIRECT_URI: {os.getenv('EBAY_REDIRECT_URI', 'NOT SET')}
EBAY_ENVIRONMENT: {os.getenv('EBAY_ENVIRONMENT', 'SANDBOX')}
        """)
        
        try:
            oauth = get_oauth_service()
            auth_url_preview = oauth.get_authorization_url(state="test")
            st.write("**Authorization URL (first 200 chars):**")
            st.code(auth_url_preview[:200] + "...")
        except Exception as e:
            st.error(f"Failed to generate auth URL: {e}")

# ============================================================================
# Footer
# ============================================================================
st.divider()
st.caption("🔒 This app securely handles eBay OAuth callbacks. No personal data is stored.")
