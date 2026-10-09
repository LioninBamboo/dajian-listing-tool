"""Quick check for READY products."""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / 'ebay_collection.db'

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT sku, title, status, suggested_price, optimization, attributes, specs "
    "FROM collected_products WHERE status IN ('READY','READY_TO_PUBLISH') ORDER BY sku"
).fetchall()

print(f"Total READY products: {len(rows)}\n")
for r in rows:
    sku = r['sku']
    title = (r['title'] or '')[:60]
    price = r['suggested_price'] or 0
    status = r['status']
    
    # Check optimization data
    opt = json.loads(r['optimization']) if r['optimization'] else {}
    opt_title = (opt.get('title', '') or '')[:60]
    cat_id = opt.get('categoryId', 'NONE')
    aspects = opt.get('aspects', {})
    has_dims = all(k in aspects for k in ['Item Length', 'Item Width', 'Item Height'])
    
    # Check attributes for source dimensions
    attrs = json.loads(r['attributes']) if r['attributes'] else {}
    has_src_dims = bool(attrs.get('Assembled Length (in.)') or attrs.get('Product Weight (lbs.)'))
    
    print(f"SKU: {sku}")
    print(f"  Status: {status} | Price: ${price}")
    print(f"  Title: {opt_title or title}")
    print(f"  CategoryId: {cat_id}")
    print(f"  Aspects: {len(aspects)} items | Has dims in aspects: {has_dims}")
    print(f"  Has source dims in attrs: {has_src_dims}")
    
    # Show dimension-related aspects
    for k in ['Item Length', 'Item Width', 'Item Height', 'Item Weight']:
        if k in aspects:
            print(f"    {k}: {aspects[k]}")
    
    # Show source dimension attrs
    for k in ['Assembled Length (in.)', 'Assembled Width (in.)', 'Assembled Height (in.)', 'Product Weight (lbs.)']:
        if k in attrs:
            print(f"    SRC {k}: {attrs[k]}")
    print()

conn.close()
