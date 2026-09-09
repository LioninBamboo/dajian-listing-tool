"""Test category suggestions for sofa"""
from src.clients.real_ebay_client import create_real_ebay_client
import os
from dotenv import load_dotenv
load_dotenv()

client = create_real_ebay_client('PRODUCTION')

# Test category suggestions for Sofa
print("=== Category Suggestions for Sofa ===")
suggestions = client.get_category_suggestions('Modular Sectional Sofa')

for i, s in enumerate(suggestions[:5]):
    cat = s.get('category', {})
    print(f"{i+1}. {cat.get('categoryName')} (ID: {cat.get('categoryId')})")

# Also test for Race Car Bed
print("\n=== Category Suggestions for Race Car Bed ===")
suggestions2 = client.get_category_suggestions('Twin Size Race Car Bed Kids')

for i, s in enumerate(suggestions2[:5]):
    cat = s.get('category', {})
    print(f"{i+1}. {cat.get('categoryName')} (ID: {cat.get('categoryId')})")
