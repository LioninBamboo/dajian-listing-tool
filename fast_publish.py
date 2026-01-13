#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fast eBay Product Publisher
直接发布产品到 eBay
"""

import os
import sys
import argparse
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, '.')

from src.clients.real_ebay_client import create_real_ebay_client

def publish(sku, title, price, qty=1, image_url=None, description=None):
    """Publish product to eBay"""
    
    print("\n" + "=" * 70)
    print(f"Publishing: {title}")
    print("=" * 70)
    
    try:
        # Initialize client
        print("[1] Initialize client...")
        client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        print("[OK] Client ready")
        
        # Check auth
        print("[2] Check authorization...")
        if not client.oauth.is_authorized():
            print("[ERROR] Not authorized")
            return False
        print("[OK] Authorized")
        
        # Create inventory
        print("[3] Create inventory...")
        product = {
            "title": title[:80],
            "description": description or f"Product: {title}",
            "price": float(price),
            "quantity": int(qty),
            "condition": "NEW",
            "image_urls": [image_url] if image_url else ["https://picsum.photos/300/300?random=1"],
            "aspects": {}
        }
        
        inv_result = client.create_or_replace_inventory_item(sku, product)
        if inv_result.get("status") != "success":
            print(f"[ERROR] Inventory failed: {inv_result.get('message')}")
            return False
        print(f"[OK] Inventory created: {sku}")
        
        # Create offer
        print("[4] Create offer...")
        offer_result = client.create_offer(sku, float(price))
        if not offer_result or "offerId" not in offer_result:
            print(f"[ERROR] Offer failed: {offer_result}")
            return False
        offer_id = offer_result.get("offerId")
        print(f"[OK] Offer created: {offer_id}")
        
        # Publish
        print("[5] Publish to eBay...")
        pub_result = client.publish_offer(offer_id)
        if not pub_result or "listingId" not in pub_result:
            print(f"[ERROR] Publish failed: {pub_result}")
            return False
        listing_id = pub_result.get("listingId")
        print(f"[OK] Published!")
        
        # Success
        print("\n" + "=" * 70)
        print("[SUCCESS] Product Published!")
        print("=" * 70)
        print(f"SKU: {sku}")
        print(f"Title: {title}")
        print(f"Price: ${price}")
        print(f"Qty: {qty}")
        print(f"Listing ID: {listing_id}")
        print(f"Offer ID: {offer_id}")
        print("=" * 70 + "\n")
        
        return True
        
    except Exception as e:
        print(f"\n[ERROR] Exception: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def main():
    parser = argparse.ArgumentParser(description='Fast eBay Publisher')
    parser.add_argument('--sku', help='SKU')
    parser.add_argument('--title', help='Product title')
    parser.add_argument('--price', type=float, help='Price')
    parser.add_argument('--qty', type=int, default=1, help='Quantity')
    parser.add_argument('--image', help='Image URL')
    parser.add_argument('--desc', help='Description')
    parser.add_argument('--demo', action='store_true', help='Demo mode')
    
    args = parser.parse_args()
    
    if args.demo or not all([args.sku, args.title, args.price]):
        # Demo mode - use alphanumeric SKU only (no hyphens!)
        return 0 if publish(
            "DEMOPUB001",
            "Demo Product - Fast Publish",
            9.99,
            5,
            description="This is a demo product"
        ) else 1
    else:
        return 0 if publish(
            args.sku,
            args.title,
            args.price,
            args.qty,
            args.image,
            args.desc
        ) else 1

if __name__ == '__main__':
    sys.exit(main())
