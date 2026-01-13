"""
eBay OAuth 2.0 Authentication Service

Implements OAuth 2.0 authorization code flow for eBay API access.
Handles token generation, refresh, and persistent storage.
"""

import os
import requests
import base64
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional
from dotenv import load_dotenv

load_dotenv()


class EbayOAuthService:
    """eBay OAuth 2.0 Service for managing API authentication"""
    
    def __init__(self, environment: str = "SANDBOX"):
        """
        Initialize OAuth service
        
        Args:
            environment: "SANDBOX" or "PRODUCTION"
        """
        self.environment = environment
        self.app_id = os.getenv("EBAY_APP_ID")
        self.cert_id = os.getenv("EBAY_CERT_ID")
        self.dev_id = os.getenv("EBAY_DEV_ID")
        self.redirect_uri = os.getenv("EBAY_REDIRECT_URI", "http://localhost:8000/ebay/callback")
        
        # API endpoints
        if environment == "PRODUCTION":
            self.auth_base = "https://auth.ebay.com/oauth2"
            self.api_base = "https://api.ebay.com"
        else:
            self.auth_base = "https://auth.sandbox.ebay.com/oauth2"
            self.api_base = "https://api.sandbox.ebay.com"
        
        # Scopes required for selling (simplified for Production)
        # Only request scopes that are typically pre-approved
        self.scopes = [
            "https://api.ebay.com/oauth/api_scope/sell.inventory",
            "https://api.ebay.com/oauth/api_scope/sell.account",
            "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
        ]
        
        # Initialize token storage
        self._init_token_db()
        # Basic validation of important env vars to help debugging
        if not self.app_id or not self.cert_id:
            print("[!] EBAY_APP_ID or EBAY_CERT_ID is not set. Check your .env or environment variables.")
        # Warn if redirect URI looks unexpected (common cause of failures)
        if self.redirect_uri and not self.redirect_uri.startswith("http://localhost"):
            print(f"[!] Warning: EBAY_REDIRECT_URI is set to '{self.redirect_uri}'. Ensure this exact URI is registered in your eBay app settings.")
    
    def _init_token_db(self):
        """Initialize SQLite database for token storage"""
        conn = sqlite3.connect("ebay_tokens.db")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                id INTEGER PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                token_type TEXT,
                expires_at TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
    
    def get_authorization_url(self, state: Optional[str] = None) -> str:
        """
        Generate eBay authorization URL for user consent
        
        Args:
            state: Optional state parameter for CSRF protection
            
        Returns:
            Authorization URL to redirect user to
        """
        params = {
            "client_id": self.app_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
        }
        
        if state:
            params["state"] = state
        
        url = f"{self.auth_base}/authorize"
        query_string = "&".join([f"{k}={requests.utils.quote(v)}" for k, v in params.items()])
        
        return f"{url}?{query_string}"
    
    def exchange_code_for_token(self, code: str) -> Dict:
        """
        Exchange authorization code for access token
        
        Supports both OAuth 2.0 and Auth'n'Auth authorization codes
        
        Args:
            code: Authorization code from callback
            
        Returns:
            Token response dict with access_token, refresh_token, etc.
        """
        print(f"[>] Exchanging authorization code...")
        print(f"   Code format: {code[:30]}...")
        print(f"   Environment: {self.environment}")
        
        # Use Identity API v1 endpoint (not oauth2)
        # This is the correct endpoint for authorization_code grant
        if self.environment == "PRODUCTION":
            url = "https://api.ebay.com/identity/v1/oauth2/token"
        else:
            url = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
        
        # Create Basic Auth header
        credentials = f"{self.app_id}:{self.cert_id}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded_credentials}"
        }
        
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri
        }
        
        print(f"   Token URL: {url}")
        print(f"   Redirect URI: {self.redirect_uri}")
        
        try:
            response = requests.post(url, headers=headers, data=data)
            
            # Log response for debugging
            print(f"   Response Status: {response.status_code}")
            
            if response.status_code != 200:
                print(f"   Response Body: {response.text}")
                
                # Try to parse error
                try:
                    error_data = response.json()
                    error_msg = error_data.get('error_description', error_data.get('error', 'Unknown error'))
                    raise Exception(f"eBay API Error: {error_msg}")
                except:
                    response.raise_for_status()
            
            token_data = response.json()
            
            print(f"[OK] Token exchange successful!")
            print(f"   Token type: {token_data.get('token_type', 'Unknown')}")
            print(f"   Expires in: {token_data.get('expires_in', 'Unknown')} seconds")
            
            # Save token to database
            self._save_token(token_data)
            
            return token_data
            
        except requests.exceptions.HTTPError as e:
            print(f"[!] HTTP Error: {e}")
            print(f"   Status Code: {e.response.status_code}")
            print(f"   Response: {e.response.text}")
            raise
        except Exception as e:
            print(f"[!] Token exchange failed: {e}")
            raise
    
    def refresh_access_token(self, refresh_token: Optional[str] = None) -> Dict:
        """
        Refresh access token using refresh token
        
        Args:
            refresh_token: Optional refresh token (uses stored if not provided)
            
        Returns:
            New token response dict
        """
        if not refresh_token:
            # Get stored refresh token
            stored_token = self._get_stored_token()
            if not stored_token or not stored_token.get("refresh_token"):
                raise ValueError("No refresh token available. Please re-authorize.")
            refresh_token = stored_token["refresh_token"]
        
        # Use the Identity API token endpoint (same endpoint used for authorization code exchange)
        if self.environment == "PRODUCTION":
            url = "https://api.ebay.com/identity/v1/oauth2/token"
        else:
            url = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
        
        credentials = f"{self.app_id}:{self.cert_id}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {encoded_credentials}"
        }
        
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(self.scopes)
        }
        
        response = requests.post(url, headers=headers, data=data)
        response.raise_for_status()
        
        token_data = response.json()
        
        # Save new token
        self._save_token(token_data)
        
        return token_data
    
    def get_valid_token(self) -> str:
        """
        Get a valid access token (auto-refresh if expired)
        
        Returns:
            Valid access token string
        """
        stored_token = self._get_stored_token()
        
        if not stored_token:
            raise ValueError("No token found. Please authorize first via /ebay/auth")
        
        # Check if token is expired
        expires_at = datetime.fromisoformat(stored_token["expires_at"])
        now = datetime.utcnow()
        
        # Refresh if expired or expiring in next 5 minutes
        if expires_at <= now + timedelta(minutes=5):
            print("[>] Token expired or expiring soon, refreshing...")
            token_data = self.refresh_access_token()
            return token_data["access_token"]
        
        return stored_token["access_token"]
    
    def _save_token(self, token_data: Dict):
        """Save token to database"""
        conn = sqlite3.connect("ebay_tokens.db")
        cursor = conn.cursor()
        
        # Calculate expiration time
        expires_in = token_data.get("expires_in", 7200)  # Default 2 hours
        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        
        # Delete old tokens
        cursor.execute("DELETE FROM oauth_tokens")
        
        # Insert new token
        cursor.execute("""
            INSERT INTO oauth_tokens (access_token, refresh_token, token_type, expires_at)
            VALUES (?, ?, ?, ?)
        """, (
            token_data["access_token"],
            token_data.get("refresh_token"),
            token_data.get("token_type", "Bearer"),
            expires_at.isoformat()
        ))
        
        conn.commit()
        conn.close()
        
        print(f"[OK] Token saved, expires at: {expires_at.isoformat()}")
    
    def _get_stored_token(self) -> Optional[Dict]:
        """Retrieve stored token from database"""
        conn = sqlite3.connect("ebay_tokens.db")
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT access_token, refresh_token, token_type, expires_at
            FROM oauth_tokens
            ORDER BY created_at DESC
            LIMIT 1
        """)
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return {
            "access_token": row[0],
            "refresh_token": row[1],
            "token_type": row[2],
            "expires_at": row[3]
        }
    
    def is_authorized(self) -> bool:
        """Check if we have a valid authorization"""
        try:
            self.get_valid_token()
            return True
        except:
            return False


# Convenience function for getting token
def get_ebay_token(environment: str = "SANDBOX") -> str:
    """
    Get a valid eBay access token
    
    Args:
        environment: "SANDBOX" or "PRODUCTION"
        
    Returns:
        Valid access token
    """
    oauth = EbayOAuthService(environment)
    return oauth.get_valid_token()


if __name__ == "__main__":
    # Test OAuth service
    oauth = EbayOAuthService()
    
    print("eBay OAuth Service Test")
    print(f"Environment: {oauth.environment}")
    print(f"App ID: {oauth.app_id[:10]}..." if oauth.app_id else "App ID: NOT SET")
    
    if oauth.is_authorized():
        print("[OK] Already authorized")
        token = oauth.get_valid_token()
        print(f"Token: {token[:20]}...")
    else:
        print("[!] Not authorized")
        print("\nAuthorization URL:")
        print(oauth.get_authorization_url())
        print("\nPlease visit this URL to authorize the application.")
