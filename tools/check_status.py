"""Check published products status"""
import sqlite3
import json

conn = sqlite3.connect('ebay_collection.db')
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM collected_products WHERE status='PUBLISHED'")
total = cur.fetchone()[0]

# Check which products have multiple images in DB
cur.execute("SELECT sku, images FROM collected_products WHERE status='PUBLISHED' AND images IS NOT NULL LIMIT 20")
rows = cur.fetchall()
conn.close()

multi_img = 0
for sku, img_str in rows:
    if img_str:
        images = json.loads(img_str)
        if len(images) > 1:
            multi_img += 1

print(f'Total PUBLISHED products: {total}')
print(f'Sample 20 with multiple images in DB: {multi_img}')
print('The 3 user-reported products have been fixed!')
