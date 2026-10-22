"""Audit today's READY SKUs one-by-one to avoid touching legacy READY."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scripts.audit_fix_ready_drafts import audit_and_fix_ready_drafts

sku_file = Path(sys.argv[1])
skus = json.loads(sku_file.read_text(encoding="utf-8"))
conn = sqlite3.connect(ROOT / "ebay_collection.db")
ph = ",".join("?" * len(skus))
ready = [
    r[0]
    for r in conn.execute(
        f"SELECT sku FROM collected_products WHERE sku IN ({ph}) AND status IN ('READY','READY_TO_PUBLISH')",
        skus,
    ).fetchall()
]
conn.close()
print(f"Auditing {len(ready)} READY SKUs from {sku_file.name}")
changed = unresolved = marked = 0
for i, sku in enumerate(ready, 1):
    print(f"[{i}/{len(ready)}] {sku}")
    report = audit_and_fix_ready_drafts(sku_filter=sku)
    changed += len(report.get("changed", []))
    unresolved += len(report.get("unresolved", []))
    marked += len(report.get("marked_ready", []))
print(json.dumps({"ready_audited": len(ready), "changed": changed, "unresolved": unresolved, "marked_ready": marked}))
