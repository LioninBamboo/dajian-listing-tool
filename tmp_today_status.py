import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sku_file = sys.argv[1]
skus = json.loads(Path(sku_file).read_text(encoding="utf-8"))
conn = sqlite3.connect("ebay_collection.db")
conn.row_factory = sqlite3.Row
ph = ",".join("?" * len(skus))
rows = conn.execute(
    f"SELECT sku, status, listing_id, suggested_price, price FROM collected_products WHERE sku IN ({ph})",
    skus,
).fetchall()
by = {r["sku"]: dict(r) for r in rows}
print("TODAY n", len(skus), "in_db", len(by))
print("today_status", dict(Counter(r["status"] for r in rows)))
print("missing", [s for s in skus if s not in by])
allc = conn.execute("SELECT status, COUNT(*) c FROM collected_products GROUP BY status").fetchall()
print("ALL_DB", {r["status"]: r["c"] for r in allc})
print("ALL_COLLECTED", conn.execute("SELECT COUNT(*) FROM collected_products WHERE status='COLLECTED'").fetchone()[0])
for s in skus:
    r = by.get(s)
    if r:
        print(f"{s}\t{r['status']}\t{r.get('listing_id') or ''}\t{r.get('suggested_price') or r.get('price')}")
    else:
        print(f"{s}\tMISSING")
