"""List or move collected products to the store instance that should sell them.

One codebase serves several stores; a product collected into the wrong instance
(auto parts landing in the furniture main store) should be moved rather than
listed there. Classification comes from src.services.product_router.

Usage:
    python tools/route_products.py --report
    python tools/route_products.py --target auto --to "C:/Users/poonx/AutoParts_Listing_Tool" --dry-run
    python tools/route_products.py --target auto --to "C:/Users/poonx/AutoParts_Listing_Tool" --move

Only products that have not been published are moved; anything already live stays
put so a move can never orphan a live listing.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.services.product_router import classify_product  # noqa: E402

MOVABLE_STATUSES = {"COLLECTED", "PENDING", "READY"}


def _rows(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _classify(row) -> dict:
    try:
        attrs = json.loads(row["attributes"] or "{}")
    except Exception:
        attrs = {}
    try:
        specs = json.loads(row["specs"] or "{}")
    except Exception:
        specs = {}
    return classify_product(
        title=row["title"] or "",
        description=row["description"] or "",
        attributes=attrs,
        supplier_category=str(specs.get("category") or ""),
    )


def report(db_path: str) -> None:
    con = _rows(db_path)
    rows = con.execute("SELECT * FROM collected_products").fetchall()
    counts, movable = Counter(), Counter()
    samples: dict[str, list] = {}
    for r in rows:
        t = _classify(r)["target"]
        counts[t] += 1
        if r["status"] in MOVABLE_STATUSES:
            movable[t] += 1
            samples.setdefault(t, []).append((r["sku"], (r["title"] or "")[:60]))
    print(f"{db_path}: {len(rows)} products")
    for target, n in counts.most_common():
        print(f"  {target:10s} {n:5d}   (movable: {movable[target]})")
    for target, items in samples.items():
        if target in ("auto", "arttoy"):
            print(f"\n  movable '{target}':")
            for sku, title in items[:15]:
                print(f"    {sku:16s} {title}")


def move(db_path: str, dest_db: str, target: str, dry_run: bool) -> None:
    src = _rows(db_path)
    rows = [r for r in src.execute("SELECT * FROM collected_products").fetchall()
            if r["status"] in MOVABLE_STATUSES and _classify(r)["target"] == target]
    if not rows:
        print(f"nothing to move for target '{target}'")
        return
    print(f"{len(rows)} product(s) classified '{target}' and movable")
    if dry_run:
        for r in rows[:20]:
            print(f"  would move {r['sku']}  {(r['title'] or '')[:60]}")
        print("(dry run — nothing written)")
        return

    dst = sqlite3.connect(dest_db)
    cols = [c[1] for c in dst.execute("PRAGMA table_info(collected_products)").fetchall()]
    moved = 0
    for r in rows:
        data = {k: r[k] for k in r.keys() if k in cols and k != "id"}
        data["status"] = "COLLECTED"      # re-optimize under the target store's brand
        data["optimization"] = None        # drop the source store's generated copy
        data["listing_id"] = None
        dst.execute("DELETE FROM collected_products WHERE sku=?", (r["sku"],))
        dst.execute(
            f"INSERT INTO collected_products ({','.join(data)}) "
            f"VALUES ({','.join('?' * len(data))})",
            list(data.values()),
        )
        src.execute("DELETE FROM collected_products WHERE sku=?", (r["sku"],))
        moved += 1
    dst.commit()
    src.commit()
    print(f"moved {moved} product(s) -> {dest_db}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="ebay_collection.db", help="source collection DB")
    ap.add_argument("--report", action="store_true", help="show routing distribution")
    ap.add_argument("--target", help="routing target to move (auto|arttoy|furniture)")
    ap.add_argument("--to", help="destination INSTANCE DIRECTORY")
    ap.add_argument("--move", action="store_true", help="actually move (default is dry run)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.report or not a.target:
        report(a.db)
        return 0
    if not a.to:
        ap.error("--to is required when moving")
    dest_db = os.path.join(a.to, "ebay_collection.db")
    if not os.path.exists(dest_db):
        ap.error(f"destination DB not found: {dest_db} (run the instance once to create it)")
    move(a.db, dest_db, a.target, dry_run=not a.move or a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
