"""Test the fixed publish flow for Green Sofa"""
import sqlite3
import json
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_publisher import EbayPublisher

print("=" * 60)
print("Testing Fixed Publish Flow for Green Sofa")
print("=" * 60)

# 1. Get product from database
conn = sqlite3.connect('ebay_collection.db')
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("SELECT * FROM collected_products WHERE sku = 'W487P411613-GreenFoam3Seat'")
row = cursor.fetchone()

if not row:
    print("Product not found!")
    exit(1)

product = dict(row)

# Parse JSON fields
for field in ['images', 'videos', 'attributes', 'specs', 'cost_breakdown', 'optimization', 'logs']:
    if product.get(field):
        try:
            product[field] = json.loads(product[field])
        except:
            pass

print(f"SKU: {product['sku']}")
print(f"Title: {product['title'][:50]}...")
print(f"Price: ${product['suggested_price']}")
print(f"Status: {product['status']}")

# 2. Initialize publisher
print("\n" + "-" * 60)
print("Initializing publisher...")
oauth = EbayOAuthService("PRODUCTION")
publisher = EbayPublisher(oauth)

# 3. Publish
print("\n" + "-" * 60)
print("Publishing...")
result = publisher.publish(product)

print("\n" + "=" * 60)
print("RESULT:")
print(f"  Status: {result.status.value}")
print(f"  Message: {result.message}")
print(f"  Listing ID: {result.listing_id}")
print(f"  Offer ID: {result.offer_id}")
print(f"  Category: {result.category_id} ({result.category_name})")
if result.errors:
    print(f"  Errors: {result.errors}")
print("=" * 60)

# 4. Update database if successful
if result.status.value == "success" and result.listing_id:
    print("\nUpdating database...")
    cursor.execute("""
        UPDATE collected_products 
        SET status = 'PUBLISHED', listing_id = ?, updated_at = datetime('now')
        WHERE sku = ?
    """, (result.listing_id, product['sku']))
    conn.commit()
    print("Database updated!")

conn.close()
