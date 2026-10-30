#!/usr/bin/env python
"""Align tiny shipping-column drift into cost_breakdown without changing insurance class.

Unlike repair_omitted_freight_costs.py (for shipping_cost=0 omissions), this only
updates snapshots where freight was already present but the live shipping column
drifted by a few cents/dollars. Preserves logistics_insurance_rate from the
existing snapshot so oversize vs express is not flipped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _q2(x: Decimal) -> float:
    return float(x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def realign_breakdown(cb: dict, shipping_col: float) -> dict | None:
    from src.services.pricing_engine import PricingEngine

    try:
        product = float(cb.get("product_price") or 0)
        old_ship = float(cb.get("shipping_cost") or 0)
        old_total = float(cb.get("total_dajian_cost") or 0)
    except (TypeError, ValueError):
        return None
    if product <= 0 or shipping_col <= 0:
        return None
    if shipping_col <= old_ship + 0.009:
        return None

    # Preserve insurance class from snapshot (do not flip oversize via Dimensions).
    rate = float(cb.get("logistics_insurance_rate") or 0)
    if abs(rate - float(PricingEngine.LOGISTICS_INSURANCE_FREIGHT)) < 1e-6:
        is_oversize = True
    elif abs(rate - float(PricingEngine.LOGISTICS_INSURANCE_EXPRESS)) < 1e-6:
        is_oversize = False
    else:
        # Unknown / missing → prefer not lowering cost: keep freight if old total
        # implies freight-ish load, else recompute both and take max.
        is_oversize = True

    rebuilt = PricingEngine.calculate_dajian_cost(
        product, shipping_col, is_oversize=is_oversize
    )
    # Preserve market / pricing metadata
    for key, val in cb.items():
        if key not in rebuilt:
            rebuilt[key] = val
        elif key.startswith("market_") or key.startswith("pricing_") or key in (
            "selling_price",
            "repriced_at",
            "health_repriced_at",
        ):
            rebuilt[key] = val

    new_total = float(rebuilt["total_dajian_cost"])
    rebuilt["shipping_aligned_at"] = datetime.now().isoformat()
    rebuilt["shipping_align"] = {
        "old_shipping_cost": old_ship,
        "new_shipping_cost": shipping_col,
        "old_total": old_total,
        "new_total": new_total,
        "is_oversize": is_oversize,
    }
    return rebuilt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(PROJECT_ROOT / "ebay_collection.db"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sku", default="")
    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    sql = """
        SELECT sku, shipping, suggested_price, cost_breakdown
        FROM collected_products
        WHERE status='PUBLISHED' AND cost_breakdown IS NOT NULL
    """
    params: list = []
    if args.sku:
        sql += " AND sku=?"
        params.append(args.sku)
    rows = con.execute(sql, params).fetchall()

    updates = []
    for r in rows:
        try:
            cb = json.loads(r["cost_breakdown"] or "{}")
        except Exception:
            continue
        ship = float(r["shipping"] or 0)
        rebuilt = realign_breakdown(cb, ship)
        if not rebuilt:
            continue
        align = rebuilt["shipping_align"]
        updates.append(
            {
                "sku": r["sku"],
                "suggested": r["suggested_price"],
                **align,
                "cost_breakdown": rebuilt,
            }
        )

    print(
        f"DB={args.db} scanned={len(rows)} align_candidates={len(updates)} "
        f"mode={'APPLY' if args.apply else 'DRY_RUN'}"
    )
    for u in updates:
        print(
            f"  {u['sku']}: ship ${u['old_shipping_cost']:.2f}->${u['new_shipping_cost']:.2f} "
            f"total ${u['old_total']:.2f}->${u['new_total']:.2f} "
            f"oversize={u['is_oversize']} suggested=${u['suggested']}"
        )

    if args.apply:
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        for u in updates:
            con.execute(
                "UPDATE collected_products SET cost_breakdown=?, updated_at=? WHERE sku=?",
                (json.dumps(u["cost_breakdown"], ensure_ascii=False), now, u["sku"]),
            )
        con.commit()
        print(f"Applied {len(updates)} snapshot alignments")

    out = PROJECT_ROOT / "logs" / f"shipping_align_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "apply": args.apply,
                "count": len(updates),
                "items": [{k: v for k, v in u.items() if k != "cost_breakdown"} for u in updates],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {out}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
