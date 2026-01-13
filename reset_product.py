import sqlite3

conn = sqlite3.connect('ebay_collection.db')
c = conn.cursor()
c.execute("UPDATE collected_products SET listing_id = NULL, status = 'READY' WHERE sku LIKE '%Dog%'")
conn.commit()
print('Reset done')
conn.close()
