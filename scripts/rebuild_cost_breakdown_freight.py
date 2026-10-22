#!/usr/bin/env python3
"""One-off: rebuild GrovePop cost_breakdown to fold real freight into landed cost.

Older snapshots stored ``cost_breakdown.shipping_cost = 0`` even though the real
freight sits in ``collected_products.shipping``. That poisoned every downstream
reader (listing margin gate, smart reprice floor). This recomputes the cost
fields via the single source of truth (PricingEngine.calculate_dajian_cost)
using ``max(stored_ship, shipping_column)`` and preserves the non-cost fields
(selling_price, market_avg_price, pricing_strategy, repriced_at, ...).

    python scripts/rebuild_cost_breakdown_freight.py            # dry-run report
    python scripts/rebuild_cost_breakdown_freight.py --apply    # write changes
"""
from __future__ import annotations
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.services.pricing_engine import PricingEngine

COST_KEYS = (
    "product_price", "shipping_cost", "base_cost",
    "return_insurance", "logistics_insurance", "payment_fee", "total_dajian_cost",
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    db = ROOT / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sku, status, shipping, specs, attributes, cost_breakdown "
        "FROM collected_products WHERE cost_breakdown IS NOT NULL"
    ).fetchall()

    total = changed = blind = 0
    old_sum = new_sum = 0.0
    pub_changed = 0
    updates = []
    for r in rows:
        try:
            cb = json.loads(r["cost_breakdown"]) if r["cost_breakdown"] else {}
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(cb, dict):
            continue
        product = float(cb.get("product_price") or 0)
        if product <= 0:
            continue
        total += 1
        stored_ship = float(cb.get("shipping_cost") or 0)
        ship_col = float(r["shipping"] or 0)
        real_ship = max(stored_ship, ship_col)
        if ship_col > 0 and stored_ship == 0:
            blind += 1
        try:
            specs = json.loads(r["specs"]) if r["specs"] else {}
        except (TypeError, json.JSONDecodeError):
            specs = {}
        try:
            attrs = json.loads(r["attributes"]) if r["attributes"] else {}
        except (TypeError, json.JSONDecodeError):
            attrs = {}
        specs = specs if isinstance(specs, dict) else {}
        attrs = attrs if isinstance(attrs, dict) else {}
        oversize = "Dimensions" in specs or "Dimensions" in attrs

        fresh = PricingEngine.calculate_dajian_cost(product, real_ship, oversize)
        old_total = float(cb.get("total_dajian_cost") or 0)
        new_total = float(fresh["total_dajian_cost"])
        if abs(new_total - old_total) < 0.005:
            continue
        merged = dict(cb)
        for k in COST_KEYS:
            merged[k] = fresh[k]
        changed += 1
        old_sum += old_total
        new_sum += new_total
        if (r["status"] or "").upper() == "PUBLISHED":
            pub_changed += 1
        updates.append((json.dumps(merged), r["sku"]))

    print(f"rows with product_price>0 : {total}")
    print(f"freight-blind (ship col>0, cb ship=0): {blind}")
    print(f"cost_breakdown changed    : {changed}  (PUBLISHED: {pub_changed})")
    print(f"sum total_dajian_cost  old ${old_sum:,.2f} -> new ${new_sum:,.2f}  (+${new_sum-old_sum:,.2f})")

    if args.apply and updates:
        conn.executemany(
            "UPDATE collected_products SET cost_breakdown = ? WHERE sku = ?", updates
        )
        conn.commit()
        print(f"APPLIED: updated {len(updates)} rows")
    elif not args.apply:
        print("DRY-RUN: no changes written (pass --apply)")
    conn.close()


if __name__ == "__main__":
    main()
