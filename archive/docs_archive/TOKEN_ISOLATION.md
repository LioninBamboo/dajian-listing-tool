# eBay Token Isolation Strategy

This guide explains how to safely develop new features or integrate with other apps while protecting the existing eBay functionality.

## The Problem

eBay OAuth tokens are stored in `ebay_tokens.db`. If multiple apps share the same token database or attempt to use the same callback URL, they can:
1. Overwrite each other's tokens
2. Cause token refresh conflicts
3. Break existing functionality

## Solution: Complete Isolation

### 1. Separate Token Storage

Each app should use its own token database file:

```python
# App 1 (this app) - ebay_tokens.db
oauth = EbayOAuthService("PRODUCTION", token_db="ebay_tokens.db")

# App 2 (new app) - ebay_tokens_app2.db  
oauth = EbayOAuthService("PRODUCTION", token_db="ebay_tokens_app2.db")
```

### 2. Unique Redirect URIs

Register a unique callback URL for each app in eBay Developer Portal:

```
App 1: http://localhost:8000/ebay/callback
App 2: http://localhost:8001/ebay/callback
App 3: http://localhost:8002/ebay/callback
```

### 3. Environment File Separation

Use different `.env` files or prefixed variables:

```bash
# .env for this app
EBAY_REDIRECT_URI=http://localhost:8000/ebay/callback

# .env.app2 for new app
EBAY_REDIRECT_URI=http://localhost:8001/ebay/callback
```

## Safe Development Patterns

### Pattern 1: Separate Folder (Recommended)

Create a completely separate project folder:

```
C:\Users\poonx\
├── Dajian_Listing_Tool/          # This app (port 8000)
│   ├── ebay_tokens.db            # This app's tokens
│   └── .env                      # This app's config
│
└── New_eBay_App/                  # New app (port 8001)
    ├── ebay_tokens_new.db        # New app's tokens
    └── .env                      # New app's config
```

### Pattern 2: Submodule Integration

If you want to use this app's eBay client in another app:

```python
import sys
sys.path.append("C:/Users/poonx/Dajian_Listing_Tool")

from src.services.ebay_auth import EbayOAuthService

# Use the SAME token DB (read-only recommended)
oauth = EbayOAuthService("PRODUCTION", token_db="C:/Users/poonx/Dajian_Listing_Tool/ebay_tokens.db")

# Check if authorized before using
if oauth.is_authorized():
    token = oauth.get_valid_token()
    # Use the token...
else:
    print("Need to authorize in the main app first")
```

### Pattern 3: Shared Token with Lock

For advanced scenarios where you need shared tokens:

```python
import fcntl
import time

def get_token_with_lock(oauth, lock_file="ebay_token.lock"):
    """Get token with file-based locking to prevent conflicts"""
    with open(lock_file, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            return oauth.get_valid_token()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
```

## Do's and Don'ts

### ✅ Do:
- Create separate token database files for each app
- Use unique callback URIs registered in eBay Developer Portal
- Test new features in a separate folder first
- Keep this app's port (8000) reserved for listing automation

### ❌ Don't:
- Modify `ebay_tokens.db` directly
- Run multiple OAuth authorization flows simultaneously
- Share the same callback URL between different apps
- Delete token databases without backing them up

## Emergency Recovery

If tokens get corrupted:

```bash
# 1. Backup corrupted tokens
copy ebay_tokens.db ebay_tokens.backup.db

# 2. Delete corrupted tokens
del ebay_tokens.db

# 3. Re-authorize via the Streamlit app
python -m streamlit run app.py --server.port 8501
# Go to eBay Authorization page and click "Authorize with eBay"
```

## Testing New Features

Before modifying the main code:

1. **Copy the project folder**
   ```bash
   xcopy /E /I Dajian_Listing_Tool Dajian_Listing_Tool_Test
   ```

2. **Rename token database**
   ```bash
   cd Dajian_Listing_Tool_Test
   ren ebay_tokens.db ebay_tokens_test.db
   ```

3. **Update .env**
   ```
   EBAY_REDIRECT_URI=http://localhost:8002/ebay/callback
   ```

4. **Make changes and test**

5. **Merge back only the code changes, NOT the databases**
