"""Test eBay Media API for video upload"""
import sys
sys.path.insert(0, '.')
from src.services.ebay_auth import EbayOAuthService
import os
import requests

environment = os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION')
print(f"Environment: {environment}")

oauth = EbayOAuthService(environment)

if not oauth.is_authorized():
    print("eBay not authorized!")
    sys.exit(1)

print("eBay Authorized: Yes")

# Get user token (Media API requires user authorization)
token = oauth.get_valid_token()
print(f"User Token obtained (first 50): {token[:50]}...")

# Test 1: Create Video (POST /commerce/media/v1_beta/video)
print("\n=== Test 1: Create Video ===")

# Correct URL: apim.ebay.com (not apiz.ebay.com)
create_video_url = "https://apim.ebay.com/commerce/media/v1_beta/video"

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

# Video metadata
video_data = {
    "title": "Cat Litter Box Enclosure Demo",
    "description": "Premium MDF and solid wood cat litter box enclosure demonstration",
    "size": 5000000,  # 5MB example
    "classification": ["ITEM"]
}

print(f"URL: {create_video_url}")
print(f"Video Title: {video_data['title']}")

response = requests.post(create_video_url, headers=headers, json=video_data)

print(f"\nStatus Code: {response.status_code}")
print(f"Response Headers:")
for key in ['x-ebay-c-requestid', 'rlogid', 'content-type']:
    if key in response.headers:
        print(f"  {key}: {response.headers[key]}")

print(f"\nResponse Body:")
print(response.text[:500] if response.text else "(empty)")

if response.status_code == 201:
    # Video ID is in the Location header
    location = response.headers.get("Location", "")
    video_id = location.split("/")[-1] if location else None
    print(f"\n=== SUCCESS! Video Resource Created ===")
    print(f"Location Header: {location}")
    print(f"Video ID: {video_id}")
    print("\nNext step: Use uploadVideo to upload the actual video content")
    
elif response.status_code == 200:
    data = response.json()
    video_id = data.get("videoId")
    print(f"\n=== SUCCESS! Video Created ===")
    print(f"Video ID: {video_id}")
    
elif response.status_code == 403:
    print("\n=== 403 Forbidden ===")
    print("Possible reasons:")
    print("  1. Media API requires additional OAuth scopes")
    print("  2. Account may need special approval for Media API")
    print("  3. Token may not have correct permissions")
    
    # Try to parse error details
    try:
        error_data = response.json()
        errors = error_data.get("errors", [])
        for err in errors:
            print(f"\n  Error ID: {err.get('errorId')}")
            print(f"  Message: {err.get('message')}")
            print(f"  Long Message: {err.get('longMessage', 'N/A')}")
    except:
        pass

elif response.status_code == 401:
    print("\n=== 401 Unauthorized ===")
    print("Token may be expired or invalid")

else:
    print(f"\n=== Error {response.status_code} ===")
    try:
        error_data = response.json()
        print(f"Errors: {error_data}")
    except:
        print(f"Raw response: {response.text}")

# Test 2: Check available scopes
print("\n\n=== Test 2: Check Token Scopes ===")
print("Current scopes in ebay_auth.py:")
for scope in oauth.scopes:
    print(f"  - {scope}")
