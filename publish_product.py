#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整的产品发布演示 - 包含所有必需字段

演示如何正确发布产品到 eBay
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, 'src')

from clients.real_ebay_client import create_real_ebay_client

def publish_product_complete():
    """完整的产品发布演示"""
    
    print("=" * 70)
    print("[*] eBay Complete Product Publishing Demo")
    print("=" * 70)
    
    # Initialize client
    print("\n[1] Initializing eBay Client...")
    try:
        client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        print("[OK] Client initialized")
    except Exception as e:
        print("[ERROR] Init failed: " + str(e))
        return False
    
    # Check auth
    print("\n[2] Checking Authorization...")
    if not client.oauth.is_authorized():
        print("[ERROR] Not authorized")
        return False
    print("[OK] Authorized")
    
    # Product data with ALL required fields
    demo_sku = "DEMO-COMPLETE-001"
    demo_product = {
        "title": "Professional LED Bulb - Bright White 3000K",
        "description": """
High Quality LED Bulb with excellent performance.

Features:
- Energy efficient LED technology
- Long lifespan: 50,000 hours
- Color temperature: 3000K (Warm White)
- Brightness: 800 lumens
- Power consumption: 10W
- Dimmable compatible

Specifications:
- Base type: E26 (Standard)
- Voltage: 110V
- CRI: 85+
- Lifespan: 50,000 hours
- Warranty: 2 years

Package includes:
- 1 x LED Bulb

100% Brand New and Original.
Fast and reliable shipping.
Full customer support.
        """.strip(),
        "price": 12.99,
        "quantity": 20,
        "condition": "NEW",
        "image_urls": [
            "https://images.unsplash.com/photo-1578272996442-48f60103fc96?w=500",
        ],  # Must provide at least one image
        "aspects": {
            "Brand": ["Generic"],
            "Type": ["Bulbs"],
            "Base Type": ["E26"],
            "Wattage": ["10W"]
        }
    }
    
    # Create inventory item
    print("\n[3] Creating Inventory Item...")
    print("    SKU: " + demo_sku)
    print("    Title: " + demo_product['title'])
    print("    Price: $" + str(demo_product['price']))
    print("    Qty: " + str(demo_product['quantity']))
    
    try:
        inv_result = client.create_or_replace_inventory_item(demo_sku, demo_product)
        print("[OK] Inventory item created/updated")
    except Exception as e:
        print("[ERROR] Inventory creation failed: " + str(e))
        return False
    
    # Create offer
    print("\n[4] Creating Offer...")
    try:
        offer_result = client.create_offer(demo_sku, demo_product["price"])
        offer_id = offer_result.get("offerId")
        
        if offer_result.get("status") == "EXISTING":
            print("[INFO] Offer already exists: " + str(offer_id))
        else:
            print("[OK] Offer created: " + str(offer_id))
    except Exception as e:
        print("[ERROR] Offer creation failed: " + str(e))
        return False
    
    # Publish offer
    print("\n[5] Publishing Offer to eBay...")
    try:
        publish_result = client.publish_offer(offer_id)
        listing_id = publish_result.get("listingId")
        print("[OK] Published successfully!")
        print("[*] Listing ID: " + str(listing_id))
    except Exception as e:
        print("[ERROR] Publishing failed: " + str(e))
        print("\n[!] Troubleshooting tips:")
        print("    1. Check if inventory item has all required fields")
        print("    2. Ensure quantity > 0")
        print("    3. Check if you exceeded daily/monthly listing limits")
        print("    4. Some categories require additional seller information")
        return False
    
    # Success!
    print("\n" + "=" * 70)
    print("[OK] SUCCESS! Product published to eBay")
    print("=" * 70)
    print("\n[*] Product Summary:")
    print("    SKU: " + demo_sku)
    print("    Title: " + demo_product['title'])
    print("    Listing ID: " + str(listing_id))
    print("    Price: $" + str(demo_product['price']))
    print("    Quantity: " + str(demo_product['quantity']))
    
    print("\n[*] Next steps:")
    print("    1. Visit eBay.com and search for your product")
    print("    2. View it in your seller account")
    print("    3. Modify SKU and product info to publish more items")
    
    return True

def publish_custom(sku, title, price, quantity=1):
    """Publish a custom product"""
    
    print("\n[*] Publishing: " + sku)
    
    try:
        client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        
        if not client.oauth.is_authorized():
            print("[ERROR] Not authorized")
            return False
        
        # Minimal product data
        product = {
            "title": title,
            "description": "Brand new product. See details above.",
            "price": price,
            "quantity": quantity,
            "condition": "NEW",
            "aspects": {"Brand": ["Generic"]}
        }
        
        # Create inventory
        client.create_or_replace_inventory_item(sku, product)
        print("[OK] Inventory created")
        
        # Create and publish offer
        offer_result = client.create_offer(sku, price)
        offer_id = offer_result.get("offerId")
        
        publish_result = client.publish_offer(offer_id)
        listing_id = publish_result.get("listingId")
        
        print("[OK] Published: " + str(listing_id))
        return True
    
    except Exception as e:
        print("[ERROR] " + str(e))
        return False

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="eBay Product Publisher")
    parser.add_argument("--demo", action="store_true", help="Run demo")
    parser.add_argument("--sku", help="Product SKU")
    parser.add_argument("--title", help="Product title")
    parser.add_argument("--price", type=float, help="Product price")
    parser.add_argument("--qty", type=int, default=1, help="Quantity")
    
    args = parser.parse_args()
    
    if args.sku and args.title and args.price:
        success = publish_custom(args.sku, args.title, args.price, args.qty)
        sys.exit(0 if success else 1)
    else:
        success = publish_product_complete()
        sys.exit(0 if success else 1)
