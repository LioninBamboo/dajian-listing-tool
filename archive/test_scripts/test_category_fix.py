"""
Test Category Suggestion Fix

This script tests the Taxonomy API to get correct category suggestions
for a Dog Crate product.
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

def get_category_suggestions_direct(title: str):
    """
    Get category suggestions using Taxonomy API with Application Token
    """
    from src.services.ebay_auth import EbayOAuthService
    
    oauth = EbayOAuthService("PRODUCTION")
    
    # Get application token (for public APIs like Taxonomy)
    token = oauth.get_application_token()
    print(f"Token (first 30 chars): {token[:30]}...")
    
    # Taxonomy API
    category_tree_id = "0"  # EBAY_US
    url = f"https://api.ebay.com/commerce/taxonomy/v1/category_tree/{category_tree_id}/get_category_suggestions"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }
    
    params = {
        "q": title
    }
    
    print(f"\nQuerying: {title}")
    print(f"URL: {url}")
    
    response = requests.get(url, headers=headers, params=params)
    
    print(f"Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        suggestions = data.get("categorySuggestions", [])
        
        print(f"\n=== Category Suggestions ({len(suggestions)} found) ===")
        for i, s in enumerate(suggestions[:5]):
            cat = s.get("category", {})
            ancestors = s.get("categoryTreeNodeAncestors", [])
            path = " > ".join([a.get("categoryName", "") for a in reversed(ancestors)])
            print(f"\n{i+1}. {cat.get('categoryName')} (ID: {cat.get('categoryId')})")
            print(f"   Path: {path}")
        
        if suggestions:
            return suggestions[0].get("category", {}).get("categoryId")
    else:
        print(f"Error: {response.text}")
        return None

def check_current_listing():
    """Check the current product in DB and its category"""
    from src.db.collection_db import get_db
    from src.db.collection_models import CollectedProduct
    
    db = next(get_db())
    
    product = db.query(CollectedProduct).filter(
        CollectedProduct.sku == "W5216S00001-WhiteWalnutMDFMetalDog"
    ).first()
    
    if product:
        print(f"\n=== Current Product Info ===")
        print(f"SKU: {product.sku}")
        print(f"Title: {product.title}")
        print(f"Listing ID: {product.listing_id}")
        print(f"Status: {product.status}")
        
        opt = product.optimization or {}
        print(f"Optimization Title: {opt.get('title', 'N/A')[:100]}")
        print(f"Current Category ID: {opt.get('categoryId', 'NOT SET')}")
        
        return product.title, opt.get('title')
    else:
        print("Product not found!")
        return None, None

def main():
    print("=" * 60)
    print("Category Suggestion Fix Test")
    print("=" * 60)
    
    # Step 1: Check current product
    original_title, opt_title = check_current_listing()
    
    # Step 2: Test with different search queries
    test_queries = [
        "71 Inch Dog Crate Furniture Style",
        "Dog Crate",
        "Dog Cage Large",
        "Furniture Style Dog Crate",
        "Pet Crate Indoor",
        "Large Dog Kennel Indoor"
    ]
    
    print("\n" + "=" * 60)
    print("Testing Category API with Different Queries")
    print("=" * 60)
    
    for query in test_queries:
        category_id = get_category_suggestions_direct(query)
        if category_id:
            print(f">>> Suggested Category ID: {category_id}")
        print("-" * 40)

if __name__ == "__main__":
    main()
