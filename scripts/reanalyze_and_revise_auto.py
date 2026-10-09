#!/usr/bin/env python3
"""Re-analyze a live Motors item with the (gated) auto_technical pipeline and push
the clean description + strengthened title via ReviseFixedPriceItem.

Fixes items published BEFORE the specialized-template appender gate landed, whose
live description still carries the furniture 'Product Dimensions' box + spec <ul>
after the auto footer. Description/title only — fitment/aspects/price untouched.

    python scripts/reanalyze_and_revise_auto.py --sku W3611P453305            # dry run
    python scripts/reanalyze_and_revise_auto.py --sku W3611P453305 --live     # push
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


def _loads(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sku", required=True)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--retries", type=int, default=5)
    args = ap.parse_args()

    conn = sqlite3.connect(str(ROOT / "ebay_collection.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM collected_products WHERE sku=?", (args.sku,)).fetchone()
    if not row:
        print(f"{args.sku}: not found"); return
    d = dict(row)
    item_id = d.get("listing_id")
    if not item_id:
        print(f"{args.sku}: no live listing_id"); return

    attrs = _loads(d.get("attributes"))
    specs = _loads(d.get("specs"))
    opt_old = _loads(d.get("optimization"))
    market_intel = None
    if isinstance(opt_old, dict):
        kws = opt_old.get("top_keywords") or (opt_old.get("market_intel") or {}).get("top_keywords")
        if kws:
            market_intel = {"top_keywords": kws}

    from qwen_optimizer import QwenOptimizer
    from src.utils.listing_fact_sheet import check_fact_sheet_violations
    opt = QwenOptimizer(api_key=os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))
    leftovers = ["f8f9fa", "Product Dimensions:", "Overall Dimensions (L x W x H)"]

    print(f"=== {args.sku}  item {item_id} ===")
    result = new_title = new_desc = None
    # The LLM varies at temp 0.4 and occasionally synthesizes an unsupported claim
    # (weather-ready, disassembles into N sections). Regenerate until the semantic
    # guard passes — never write a description that failed the guard.
    for attempt in range(1, args.retries + 1):
        result = opt.optimize_auto_technical_listing(
            d.get("title") or "", d.get("description") or "",
            attributes=attrs, specs=specs, market_intel=market_intel,
        )
        if result.get("error"):
            print(f"  attempt {attempt}: generation FAILED: {result.get('error')}"); continue
        new_title = result.get("title", "")
        new_desc = result.get("description", "")
        hits = [s for s in leftovers if s in new_desc]
        try:
            fc = sqlite3.connect(str(ROOT / "fact_sheet_cache.db"))
            v = check_fact_sheet_violations(
                fc, d.get("title") or "", d.get("description") or "", attrs, specs,
                new_title, new_desc, result.get("aspects") or {},
            )
            fc.close()
            status = (v or {}).get("status")
            viols = (v or {}).get("violations") or []
        except Exception as e:
            status, viols = "unavailable", []
            print(f"  fact-guard skipped: {e}")
        print(f"  attempt {attempt}: title({len(new_title)}) desc({len(new_desc)}) "
              f"leftover={hits or 'NONE'} fact-guard={status}/{len(viols)}")
        if viols:
            for b in viols[:4]:
                print("     -", b.get("claim_text"), f"({b.get('severity')})")
        if not hits and status != "violations":
            break
    else:
        print("ABORT: no clean generation within retries"); return

    print(f"CHOSEN TITLE: {new_title}")
    print("--- desc tail ---"); print(new_desc[-300:])

    if not args.live:
        print("\nDRY RUN — rerun with --live to push ReviseFixedPriceItem")
        return

    from src.clients.real_ebay_client import create_real_ebay_client
    ebay = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper())

    # Motors parts live via Trading (ReviseFixedPriceItem); regular-category parts
    # live via the Inventory API (no Trading revise — eBay rejects it). Detect by
    # whether the SKU has a Sell offer, then take the matching update path.
    offers = ebay.get_offers_by_sku(args.sku) or []
    if offers:
        offer = offers[0]
        offer_id = offer.get("offerId")
        cat = offer.get("categoryId") or (ebay.get_offer(offer_id) or {}).get("categoryId")
        inv = ebay.get_inventory_item(args.sku) or {}
        prod = inv.get("product", {}) or {}
        qty = (((inv.get("availability") or {}).get("shipToLocationAvailability") or {}).get("quantity"))
        product = {
            "title": new_title,
            "description": new_desc,                      # inventory_item copy (offer overrides live)
            "image_urls": prod.get("imageUrls", []),      # preserve existing media
            "aspects": prod.get("aspects", {}),           # preserve live item specifics
        }
        if qty is not None:
            product["quantity"] = qty
        ebay.create_or_replace_inventory_item(args.sku, product)
        ebay.update_offer_category(offer_id, str(cat), listing_description=new_desc)
        pub = ebay.publish_offer(offer_id)
        res = {"itemId": item_id, "path": "inventory", "offerId": offer_id, "publish": pub}
        print("INVENTORY UPDATE:", res)
    else:
        res = ebay.revise_fixed_price_item_motors(str(item_id), description=new_desc, title=new_title)
        res["path"] = "trading"
        print("REVISE:", res)

    # Persist the clean optimization locally.
    if isinstance(opt_old, dict):
        opt_old["title"] = new_title
        opt_old["description"] = new_desc
        opt_old.setdefault("logs", [])
    logs = _loads(d.get("logs")) or []
    if not isinstance(logs, list):
        logs = []
    logs.append({"event": "reanalyze_revise_auto", "at": datetime.now(timezone.utc).isoformat(),
                 "item": item_id, "path": res.get("path"), "ack": res.get("ack")})
    conn.execute("UPDATE collected_products SET optimization=?, logs=?, updated_at=? WHERE sku=?",
                 (json.dumps(opt_old, ensure_ascii=False), json.dumps(logs, ensure_ascii=False),
                  datetime.now(timezone.utc).isoformat(), args.sku))
    conn.commit()
    print("local DB updated ✓")
    conn.close()


if __name__ == "__main__":
    main()
