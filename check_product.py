import sqlite3
import json

conn = sqlite3.connect('ebay_collection.db')
c = conn.cursor()
c.execute('SELECT sku, title, optimization, images FROM collected_products WHERE sku LIKE "%WhiteWalnut%"')
row = c.fetchone()

print("SKU:", row[0])
print("Title:", row[1][:80] if row[1] else None)

opt = json.loads(row[2]) if row[2] else {}
print("\n--- Optimization Data ---")
print("Opt Title:", opt.get('title', 'NONE'))
print("Has description:", bool(opt.get('description')))
print("Aspects:", opt.get('aspects', {}))

images = json.loads(row[3]) if row[3] else []
print("\n--- Images ---")
print("Image count:", len(images))
if images:
    print("First image:", images[0][:100] if len(images[0]) > 100 else images[0])
