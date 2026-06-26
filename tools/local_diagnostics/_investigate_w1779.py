import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / 'ebay_collection.db'

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("SELECT sku, title, attributes, specs, optimization, status, description FROM collected_products WHERE sku LIKE '%W1779%' OR sku LIKE '%440173%'")
rows = cur.fetchall()
for r in rows:
    print('=== SKU:', r['sku'], '===')
    print('Title:', r['title'])
    print('Status:', r['status'])
    attrs = r['attributes']
    if attrs:
        attrs_d = json.loads(attrs) if isinstance(attrs, str) else attrs
        print('Attributes:')
        for k, v in attrs_d.items():
            print(f'  {k}: {v}')
    specs = r['specs']
    if specs:
        specs_d = json.loads(specs) if isinstance(specs, str) else specs
        print('Specs:')
        for k, v in specs_d.items():
            print(f'  {k}: {v}')
    if r['optimization']:
        opt = json.loads(r['optimization']) if isinstance(r['optimization'], str) else r['optimization']
        print('Category ID:', opt.get('categoryId'))
        print('Optimized Title:', opt.get('title'))
        aspects = opt.get('aspects', {})
        print('Aspects:')
        for k, v in aspects.items():
            print(f'  {k}: {v}')
    desc = r['description']
    if desc:
        print('Description (first 1000 chars):', desc[:1000])
    print()
conn.close()
