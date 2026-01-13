#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取您前端活跃 listing 的完整结构用作参考
参考已成功的 listing，与 API 创建的 Offer 进行对比
"""

import os
import sys
from dotenv import load_dotenv
import requests
import json

load_dotenv()
sys.path.insert(0, '.')

from src.services.ebay_auth import EbayOAuthService

def get_all_offers():
    """获取所有 Offers"""
    
    auth = EbayOAuthService()
    token = auth.get_valid_token()
    
    if not token:
        print("[ERROR] No token")
        return None
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    # Get all offers
    url = 'https://api.ebay.com/sell/inventory/v1/offer'
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.json()
    else:
        print(f"[ERROR] {response.status_code}: {response.text}")
        return None

def get_active_listings():
    """获取活跃 listings（已发布）"""
    
    auth = EbayOAuthService()
    token = auth.get_valid_token()
    
    if not token:
        print("[ERROR] No token")
        return None
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }
    
    # Get active listings using different endpoint
    # This might require Get Listing API
    url = 'https://api.ebay.com/sell/listing/v1/item'
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        print("[INFO] Listing API endpoint not available")
        return None
    else:
        print(f"[ERROR] {response.status_code}: {response.text}")
        return None

def main():
    print("=" * 80)
    print("Fetching Your Account's Offers and Listings for Reference")
    print("=" * 80)
    
    # Get all offers
    print("\n[1] Getting all Offers (both PUBLISHED and UNPUBLISHED)...")
    print("-" * 80)
    
    offers_data = get_all_offers()
    
    if offers_data:
        offers = offers_data.get('offers', [])
        print(f"[OK] Found {len(offers)} total offers")
        
        # Group by status
        published = [o for o in offers if o.get('status') == 'PUBLISHED']
        unpublished = [o for o in offers if o.get('status') == 'UNPUBLISHED']
        
        print(f"  - Published (Live): {len(published)}")
        print(f"  - Unpublished (Drafts): {len(unpublished)}")
        
        # Show details of published offers
        if published:
            print(f"\n[PUBLISHED LISTINGS] (These are your active ones)")
            print("-" * 80)
            
            for offer in published[:3]:  # Show first 3
                print(f"\nOffer ID: {offer.get('offerId')}")
                print(f"  SKU: {offer.get('sku')}")
                print(f"  Status: {offer.get('status')}")
                print(f"  ListingStatus: {offer.get('listingStatus')}")
                print(f"  Price: {offer.get('pricingSummary', {}).get('price', {}).get('value')}")
                print(f"  Policies: {bool(offer.get('listingPolicies'))}")
                print(f"  Keys: {list(offer.keys())}")
        
        # Show details of unpublished offers
        if unpublished:
            print(f"\n[UNPUBLISHED DRAFTS] (Ones we created)")
            print("-" * 80)
            
            for offer in unpublished[:3]:  # Show first 3
                print(f"\nOffer ID: {offer.get('offerId')}")
                print(f"  SKU: {offer.get('sku')}")
                print(f"  Status: {offer.get('status')}")
                print(f"  ListingStatus: {offer.get('listingStatus')}")
                print(f"  Price: {offer.get('pricingSummary', {}).get('price', {}).get('value')}")
                print(f"  Policies: {bool(offer.get('listingPolicies'))}")
                print(f"  Keys: {list(offer.keys())}")
        
        # Compare
        if published and unpublished:
            print(f"\n[COMPARISON]")
            print("-" * 80)
            
            pub_keys = set(published[0].keys())
            unpub_keys = set(unpublished[0].keys())
            
            missing_in_unpub = pub_keys - unpub_keys
            extra_in_unpub = unpub_keys - pub_keys
            
            if missing_in_unpub:
                print(f"Missing in unpublished (we created): {missing_in_unpub}")
            if extra_in_unpub:
                print(f"Extra in unpublished: {extra_in_unpub}")
            
            if not missing_in_unpub and not extra_in_unpub:
                print("Structure is identical!")
        
        # Save full JSON for inspection
        print(f"\n[SAVING] Full JSON to offers_data.json")
        with open('offers_data.json', 'w') as f:
            json.dump(offers_data, f, indent=2)
        
    else:
        print("[ERROR] Could not fetch offers")
    
    # Try to get listings
    print("\n\n[2] Trying Get Listing API...")
    print("-" * 80)
    
    listings_data = get_active_listings()
    
    if listings_data:
        print(f"[OK] Got listings data")
        with open('listings_data.json', 'w') as f:
            json.dump(listings_data, f, indent=2)
        print("Saved to listings_data.json")
    
    print("\n" + "=" * 80)
    print("Files created:")
    print("  - offers_data.json (all your offers)")
    print("  - listings_data.json (if available)")
    print("\nReview these files to see:")
    print("  1. What fields active listings have")
    print("  2. What we're missing in our API-created offers")
    print("=" * 80)

if __name__ == '__main__':
    main()
