"""Write today's READY sku-list text + comma list for batch_publish."""
import json
import sqlite3
import sys
from pathlib import Path

sku_file = Path(sys.argv[1])
out_txt = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("cache/today_ready_skus.txt")
skus = json.loads(sku_file.read_text(encoding="utf-8"))
conn = sqlite3.connect("ebay_collection.db")
ph = ",".join("?" * len(skus))
ready = [
    r[0]
    for r in conn.execute(
        f"SELECT sku FROM collected_products WHERE sku IN ({ph}) AND status IN ('READY','READY_TO_PUBLISH') ORDER BY sku",
        skus,
    ).fetchall()
]
# skip N735* and cargo>$400
rows = conn.execute(
    f"SELECT sku, price FROM collected_products WHERE sku IN ({ph})",
    skus,
).fetchall()
price_by = {r[0]: float(r[1] or 0) for r in rows}
conn.close()
filtered = []
skipped = []
for s in ready:
    if any(str(s).upper().startswith(_p) for _p in ("N735", "W1706", "ZL")):
        skipped.append((s, "N735_blacklist"))
        continue
    if price_by.get(s, 0) > 400:
        skipped.append((s, f"cargo>{price_by[s]}"))
        continue
    filtered.append(s)
out_txt.write_text("\n".join(filtered) + ("\n" if filtered else ""), encoding="utf-8")
print("ready", len(ready), "publish_candidates", len(filtered), "skipped", skipped)
print("sku_list_csv_len", len(",".join(filtered)))
print("OUT", out_txt)
