import sqlite3
import json

def parse_json(raw):
    if raw is None: return {}
    try: return json.loads(raw)
    except: return {}

def main():
    conn = sqlite3.connect('ebay_collection.db')
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT sku, optimization FROM collected_products WHERE status='READY'").fetchall()
    
    for row in rows:
        opt = parse_json(row['optimization'])
        aspects = opt.get('aspects', {})
        print(f"--- SKU: {row['sku']} ---")
        print(f"Item Length: {aspects.get('Item Length', 'MISSING')}")
        print(f"Item Width: {aspects.get('Item Width', 'MISSING')}")
        print(f"Item Height: {aspects.get('Item Height', 'MISSING')}")
        print(f"Item Weight: {aspects.get('Item Weight', 'MISSING')}")
        print(f"Assembly Required: {aspects.get('Assembly Required', 'MISSING')}")
        
if __name__ == '__main__':
    main()
