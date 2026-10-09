"""Test new luxury HTML template"""
from qwen_optimizer import QwenOptimizer
import os
from dotenv import load_dotenv
load_dotenv()

qwen = QwenOptimizer()
result = qwen.optimize_product_full(
    original_title='Large Double Dog Crate with Drawers Walnut Tabletop',
    original_description='Premium dog kennel with waterproof walnut tabletop and fluted drawers. MDF construction with metal and steel frame.',
    attributes={'Material': 'MDF, Metal, Steel', 'Color': 'White, Walnut', 'Weight': '134.7 lbs'}
)

print('=== TITLE ===')
title = result.get('title', '')
print(title)
print(f'Length: {len(title)} chars')
print()

print('=== BRAND ===')
brand = result.get('aspects', {}).get('Brand')
print(brand)
print()

print('=== HTML PREVIEW (first 1000 chars) ===')
html = result.get('description', '')
print(html[:1000])
print()
print(f'=== TOTAL HTML LENGTH: {len(html)} chars ===')

# Check if AquaVerve is in HTML header
if 'AQUAVERVE' in html.upper():
    print('✅ AquaVerve brand header found!')
else:
    print('❌ AquaVerve brand header NOT found')
