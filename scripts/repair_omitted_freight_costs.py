#!/usr/bin/env python
"""Repair PUBLISHED listings whose cost_breakdown omitted GIGA freight.

1) Rebuild cost_breakdown with max(cb.shipping_cost, collected_products.shipping)
2) Raise suggested_price to at least 10% safe floor (ad=5%) when underwater
3) Optionally push the new price to live eBay (--apply-live)

Usage:
  python scripts/repair_omitted_freight_costs.py --dry-run
  python scripts/repair_omitted_freight_costs.py --apply-db
  python scripts/repair_omitted_freight_costs.py --apply-db --apply-live
  python scripts/repair_omitted_freight_costs.py --db path/to/ebay_collection.db --apply-db
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")


def _parse_json(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def rebuild_row(row: sqlite3.Row, pricing_engine) -> dict | None:
    sku = row["sku"]
    cb = _parse_json(row["cost_breakdown"])
    attrs = _parse_json(row["attributes"])
    specs = _parse_json(row["specs"])
    shipping = float(row["shipping"] or 0)
    product_price = float(cb.get("product_price") or row["price"] or 0)
    stored_ship = float(cb.get("shipping_cost") or 0)
    old_total = float(cb.get("total_dajian_cost") or 0)
    suggested = float(row["suggested_price"] or 0)

    if product_price <= 0:
        return None

    ship = max(stored_ship, shipping)
    # Only repair when the shipping column exposes freight missing from the snapshot.
    if shipping <= stored_ship + 0.009:
        return None

    oversize = "Dimensions" in attrs or "Dimensions" in specs
    rebuilt = pricing_engine.calculate_dajian_cost(
        product_price, ship, is_oversize=oversize
    )
    for key in (
        "market_price",
        "market_avg_price",
        "market_source",
        "market_sample_size",
        "market_auth_mode",
        "market_fallback_reason",
        "pricing_strategy",
        "pricing_margin",
        "pricing_margin_basis",
        "selling_price",
        "repriced_at",
        "health_repriced_at",
    ):
        if key in cb:
            rebuilt[key] = cb[key]

    new_total = float(rebuilt["total_dajian_cost"])
    if new_total <= old_total + 0.01:
        # Freight present in column but already reflected somehow — skip.
        return None
    # FLOOR strategy ≈ 10% net-on-cost buffer with ads on.
    floor = pricing_engine.safe_floor_price(new_total, safety_margin=0.10)
    new_price = suggested
    price_action = "keep"
    if suggested + 0.01 < floor:
        new_price = floor
        price_action = "raise_to_safe10"
    rebuilt["selling_price"] = new_price
    rebuilt["freight_repaired_at"] = datetime.now().isoformat()
    rebuilt["freight_repair"] = {
        "old_shipping_cost": stored_ship,
        "new_shipping_cost": ship,
        "old_total": old_total,
        "new_total": new_total,
        "old_suggested": suggested,
        "new_suggested": new_price,
    }

    return {
        "sku": sku,
        "listing_id": row["listing_id"],
        "old_total": old_total,
        "new_total": new_total,
        "old_ship": stored_ship,
        "new_ship": ship,
        "old_suggested": suggested,
        "new_suggested": new_price,
        "floor_safe10": floor,
        "price_action": price_action,
        "cost_breakdown": rebuilt,
        "status": row["status"],
    }


def apply_live_price(sku: str, listing_id, price: float) -> tuple[bool, str]:
    try:
        from scripts.batch_smart_reprice import (
            get_ebay_client,
            update_ebay_price,
            verify_ebay_price_update,
        )
    except Exception as exc:
        return False, f"import_failed: {exc}"

    try:
        _client, oauth = get_ebay_client()
        ok = update_ebay_price(
            oauth, sku, price, expected_listing_id=listing_id
        )
        if not ok:
            # Common for already-ended / delisted items — treat as skippable.
            return False, "update_ebay_price_returned_false"
        verified, live = verify_ebay_price_update(
            oauth, sku, price, expected_listing_id=listing_id
        )
        if not verified:
            return False, f"verify_failed live={live}"
        return True, f"live={live}"
    except Exception as exc:
        msg = str(exc)
        if "ended listing" in msg.lower() or "not allowed to revise ended" in msg.lower():
            return False, "ended_listing"
        return False, msg[:200]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "ebay_collection.db"),
        help="Path to ebay_collection.db",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only (default)")
    parser.add_argument("--apply-db", action="store_true", help="Write repaired cost/price to DB")
    parser.add_argument(
        "--apply-live",
        action="store_true",
        help="Also push raised prices to eBay (requires --apply-db unless --push-raised-live)",
    )
    parser.add_argument(
        "--push-raised-live",
        action="store_true",
        help="Push already-repaired rows (freight_repair.raise) to live eBay only",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sku", default="", help="Single SKU filter")
    args = parser.parse_args()

    if args.apply_live and not args.apply_db and not args.push_raised_live:
        print("--apply-live requires --apply-db (or use --push-raised-live)")
        return 2

    # Default to dry-run when neither flag set.
    dry_run = not args.apply_db and not args.push_raised_live

    from src.services.pricing_engine import PricingEngine

    db_path = Path(args.db)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    if args.push_raised_live:
        sql = """
            SELECT sku, status, listing_id, price, shipping, suggested_price,
                   cost_breakdown, attributes, specs
            FROM collected_products
            WHERE status = 'PUBLISHED'
              AND cost_breakdown LIKE '%freight_repair%'
        """
        params: list = []
        if args.sku:
            sql += " AND sku = ?"
            params.append(args.sku)
        rows = con.execute(sql, params).fetchall()
        repairs = []
        for row in rows:
            cb = _parse_json(row["cost_breakdown"])
            fr = cb.get("freight_repair") or {}
            old_s = float(fr.get("old_suggested") or 0)
            new_s = float(row["suggested_price"] or fr.get("new_suggested") or 0)
            if new_s <= old_s + 0.01:
                continue
            repairs.append(
                {
                    "sku": row["sku"],
                    "listing_id": row["listing_id"],
                    "old_total": fr.get("old_total"),
                    "new_total": fr.get("new_total"),
                    "old_ship": fr.get("old_shipping_cost"),
                    "new_ship": fr.get("new_shipping_cost"),
                    "old_suggested": old_s,
                    "new_suggested": new_s,
                    "floor_safe10": new_s,
                    "price_action": "raise_to_safe10",
                    "cost_breakdown": cb,
                    "status": row["status"],
                }
            )
            if args.limit and len(repairs) >= args.limit:
                break
        print(
            f"DB={db_path} push_raised_live candidates={len(repairs)}"
        )
        live_ok = live_fail = 0
        out_rows = []
        for r in repairs:
            print(
                f"  LIVE {r['sku']}: ${r['old_suggested']:.2f}->${r['new_suggested']:.2f}"
            )
            ok, msg = apply_live_price(r["sku"], r["listing_id"], r["new_suggested"])
            live_status = "ok" if ok else f"fail:{msg}"
            if ok:
                live_ok += 1
            else:
                live_fail += 1
                print(f"    LIVE FAIL {r['sku']}: {msg}")
            out_rows.append({**{k: v for k, v in r.items() if k != "cost_breakdown"}, "live": live_status})
        con.close()
        out = PROJECT_ROOT / "logs" / f"freight_repair_live_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(
            json.dumps(
                {
                    "db": str(db_path),
                    "mode": "push_raised_live",
                    "live_ok": live_ok,
                    "live_fail": live_fail,
                    "items": out_rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Wrote {out}")
        print(f"Live pushes: ok={live_ok} fail={live_fail}")
        return 0 if live_fail == 0 else 1

    sql = """
        SELECT sku, status, listing_id, price, shipping, suggested_price,
               cost_breakdown, attributes, specs
        FROM collected_products
        WHERE status = 'PUBLISHED'
          AND cost_breakdown IS NOT NULL
    """
    params = []
    if args.sku:
        sql += " AND sku = ?"
        params.append(args.sku)
    sql += " ORDER BY sku"
    rows = con.execute(sql, params).fetchall()

    repairs = []
    for row in rows:
        item = rebuild_row(row, PricingEngine)
        if item:
            repairs.append(item)
        if args.limit and len(repairs) >= args.limit:
            break

    raise_n = sum(1 for r in repairs if r["price_action"] != "keep")
    print(
        f"DB={db_path} published_scanned={len(rows)} "
        f"freight_repairs={len(repairs)} price_raises={raise_n} "
        f"mode={'DRY_RUN' if dry_run else 'APPLY_DB'}"
        f"{'+LIVE' if args.apply_live else ''}"
    )

    out_rows = []
    live_ok = live_fail = 0
    for r in repairs:
        print(
            f"  {r['sku']}: cost ${r['old_total']:.2f}->${r['new_total']:.2f} "
            f"(ship ${r['old_ship']:.2f}->${r['new_ship']:.2f}) "
            f"price ${r['old_suggested']:.2f}->${r['new_suggested']:.2f} "
            f"[{r['price_action']}]"
        )
        if not dry_run:
            con.execute(
                "UPDATE collected_products SET cost_breakdown = ?, suggested_price = ?, "
                "updated_at = ? WHERE sku = ?",
                (
                    json.dumps(r["cost_breakdown"], ensure_ascii=False),
                    r["new_suggested"],
                    datetime.now().isoformat(sep=" ", timespec="seconds"),
                    r["sku"],
                ),
            )
        live_status = "skipped"
        if args.apply_live and r["price_action"] != "keep":
            ok, msg = apply_live_price(r["sku"], r["listing_id"], r["new_suggested"])
            live_status = "ok" if ok else f"fail:{msg}"
            if ok:
                live_ok += 1
            else:
                live_fail += 1
                print(f"    LIVE FAIL {r['sku']}: {msg}")
        out_rows.append({**{k: v for k, v in r.items() if k != "cost_breakdown"}, "live": live_status})

    if not dry_run:
        con.commit()
    con.close()

    out = PROJECT_ROOT / "logs" / f"freight_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "db": str(db_path),
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "dry_run": dry_run,
                "apply_live": args.apply_live,
                "count": len(out_rows),
                "price_raises": raise_n,
                "live_ok": live_ok,
                "live_fail": live_fail,
                "items": out_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {out}")
    if args.apply_live:
        print(f"Live pushes: ok={live_ok} fail={live_fail}")
    return 0 if live_fail == 0 else 1


if __name__ == "__main__":
    # Allow `from scripts.batch_smart_reprice import ...`
    os.chdir(PROJECT_ROOT)
    raise SystemExit(main())
