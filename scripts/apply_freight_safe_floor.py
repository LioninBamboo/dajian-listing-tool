#!/usr/bin/env python3
"""Raise live listings that sit below the freight-aware break-even floor.

Only lifts price up to PricingEngine.safe_floor_price (5% net buffer).
Does not follow the market and does not lower any price.

Usage:
  python scripts/apply_freight_safe_floor.py              # dry-run
  python scripts/apply_freight_safe_floor.py --sku SKU    # one SKU dry-run
  python scripts/apply_freight_safe_floor.py --apply --sku SKU
  python scripts/apply_freight_safe_floor.py --apply
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from src.services.pricing_engine import PricingEngine

log = logging.getLogger("apply_freight_safe_floor")


def _load_reprice_module():
    spec = importlib.util.spec_from_file_location(
        "grove_batch_smart_reprice",
        PROJECT_ROOT / "scripts" / "batch_smart_reprice.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_json(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_floor_candidates(db_path: Path, sku: str | None = None) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT sku, title, shipping, listing_id, cost_breakdown, specs, attributes
        FROM collected_products
        WHERE status = 'PUBLISHED' AND cost_breakdown IS NOT NULL
    """
    params: list[Any] = []
    if sku:
        sql += " AND sku = ?"
        params.append(sku)
    sql += " ORDER BY sku"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    out: list[dict] = []
    for row in rows:
        cost = _parse_json(row["cost_breakdown"])
        specs = _parse_json(row["specs"])
        attrs = _parse_json(row["attributes"])
        landed = PricingEngine.resolve_landed_cost(
            cost_breakdown=cost,
            shipping_fallback=row["shipping"] or 0,
            specs=specs,
            attributes=attrs,
        ) or 0
        if landed <= 0:
            continue
        out.append(
            {
                "sku": row["sku"],
                "title": row["title"] or "",
                "listing_id": row["listing_id"],
                "landed": float(landed),
                "abs_floor": PricingEngine.absolute_floor_price(landed),
                "safe_floor": PricingEngine.safe_floor_price(landed, 0.05),
                "cost_breakdown": cost,
            }
        )
    return out


def write_local_price(db_path: Path, sku: str, cost_data: dict, new_price: float) -> None:
    payload = dict(cost_data)
    payload["selling_price"] = new_price
    payload["pricing_strategy"] = "FREIGHT_SAFE_FLOOR"
    payload["repriced_at"] = datetime.now().isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE collected_products SET cost_breakdown = ?, suggested_price = ? WHERE sku = ?",
        (json.dumps(payload, ensure_ascii=False), new_price, sku),
    )
    conn.commit()
    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Raise below-floor GrovePop listings to safe floor")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sku", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "ebay_collection.db"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    db_path = Path(args.db)
    candidates = load_floor_candidates(db_path, sku=args.sku or None)
    if not candidates:
        print("No published candidates.")
        return 0

    reprice = _load_reprice_module()
    from src.services.ebay_auth import EbayOAuthService

    oauth = EbayOAuthService("PRODUCTION")
    below: list[dict] = []
    skipped = 0
    live_missing = 0
    for item in candidates:
        live = reprice.fetch_live_offer_price(
            oauth, item["sku"], expected_listing_id=item.get("listing_id")
        )
        item["live"] = live
        if live is None:
            live_missing += 1
            log.warning("%s: no live offer price", item["sku"])
            continue
        if live + 0.01 >= item["abs_floor"]:
            skipped += 1
            continue
        item["new_price"] = item["safe_floor"]
        item["gap"] = round(item["safe_floor"] - live, 2)
        below.append(item)
        time.sleep(0.15)

    below.sort(key=lambda row: row["gap"], reverse=True)
    if args.limit > 0:
        below = below[: args.limit]

    print(
        f"{'APPLY' if args.apply else 'DRY-RUN'} candidates={len(candidates)} "
        f"below_abs_floor={len(below)} already_ok={skipped} live_missing={live_missing}"
    )
    for item in below[:20]:
        print(
            f"  {item['sku']} live=${item['live']:.2f} -> ${item['new_price']:.2f} "
            f"(+{item['gap']:.2f}) abs=${item['abs_floor']:.2f} landed=${item['landed']:.2f}"
        )
    if len(below) > 20:
        print(f"  ... {len(below) - 20} more")

    if not args.apply:
        return 0

    updated = 0
    failed = 0
    for i, item in enumerate(below, 1):
        sku = item["sku"]
        new_price = float(item["new_price"])
        ok = reprice.update_ebay_price(oauth, sku, new_price, expected_listing_id=item.get("listing_id"))
        if not ok:
            failed += 1
            log.error("[%s/%s] %s: update failed", i, len(below), sku)
            continue
        verified, live_price = reprice.verify_ebay_price_update(
            oauth, sku, new_price, expected_listing_id=item.get("listing_id")
        )
        if not verified:
            failed += 1
            log.error(
                "[%s/%s] %s: verify failed live=%s expected=%.2f",
                i,
                len(below),
                sku,
                live_price,
                new_price,
            )
            continue
        write_local_price(db_path, sku, item["cost_breakdown"], new_price)
        updated += 1
        log.info("[%s/%s] %s: $%.2f -> $%.2f verified", i, len(below), sku, item["live"], new_price)
        time.sleep(1.5)

    print(f"done: updated={updated} failed={failed} targeted={len(below)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
