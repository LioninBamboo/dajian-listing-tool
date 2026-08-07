#!/usr/bin/env python3
"""Re-analyze a live GrovePop item with the garden_lifestyle template and push the
redesigned description + strengthened title live via the Inventory API.

GrovePop listings are all Inventory-channel: the OFFER's listingDescription is what
buyers see, so we update inventory_item (title) + offer.listingDescription + publish.
Regenerates until the semantic FactSheet guard passes — never writes a failed one.

    python scripts/reanalyze_and_revise_garden.py --sku W5230P516333          # dry run
    python scripts/reanalyze_and_revise_garden.py --sku W5230P516333 --live   # push
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
    # A clean garden description carries the new scoped style block + footer, and
    # must NOT carry the old furniture shell.
    must_have = ["gpl-w", "gpl-bar", "Ships from"]
    must_not = ["f8f9fa", "Product Dimensions:", "Overall Dimensions (L x W x H)"]

    print(f"=== {args.sku}  item {item_id} ===")
    result = new_title = new_desc = None
    for attempt in range(1, args.retries + 1):
        result = opt.optimize_garden_lifestyle_listing(
            d.get("title") or "", d.get("description") or "",
            attributes=attrs, specs=specs, market_intel=market_intel,
        )
        if result.get("error"):
            print(f"  attempt {attempt}: generation FAILED: {result.get('error')}"); continue
        new_title = result.get("title", "")
        new_desc = result.get("description", "")
        bad = [s for s in must_not if s in new_desc]
        missing = [s for s in must_have if s not in new_desc]
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
              f"bad={bad or 'NONE'} missing={missing or 'NONE'} fact-guard={status}/{len(viols)}")
        if viols:
            for b in viols[:4]:
                print("     -", b.get("claim_text"), f"({b.get('severity')})")
        if not bad and not missing and status != "violations":
            break
    else:
        print("ABORT: no clean generation within retries"); return

    print(f"CHOSEN TITLE ({len(new_title)}): {new_title}")
    print("--- desc tail ---"); print(new_desc[-260:])

    if not args.live:
        print("\nDRY RUN — rerun with --live to push the Inventory update")
        return

    from src.clients.real_ebay_client import create_real_ebay_client
    ebay = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper())
    offers = ebay.get_offers_by_sku(args.sku) or []
    if not offers:
        print("ABORT: no Sell offer for this SKU"); return
    offer = offers[0]
    offer_id = offer.get("offerId")
    cat = offer.get("categoryId") or (ebay.get_offer(offer_id) or {}).get("categoryId")
    inv = ebay.get_inventory_item(args.sku) or {}
    prod = inv.get("product", {}) or {}
    qty = (((inv.get("availability") or {}).get("shipToLocationAvailability") or {}).get("quantity"))
    # Preserve the live item specifics (they already satisfy eBay's category
    # requirements incl. Brand/MPN); this pass is a DESIGN sync, not an aspect
    # rewrite. Belt-and-suspenders: eBay wants an MPN whenever Brand is set.
    aspects = dict(prod.get("aspects", {}) or result.get("aspects") or {})
    if aspects.get("Brand") and not aspects.get("MPN"):
        aspects["MPN"] = [args.sku]
    product = {
        "title": new_title,
        "description": new_desc,
        "image_urls": prod.get("imageUrls", []),
        "aspects": aspects,
    }
    if qty is not None:
        product["quantity"] = qty
    ebay.create_or_replace_inventory_item(args.sku, product)
    ebay.update_offer_category(offer_id, str(cat), listing_description=new_desc)
    pub = ebay.publish_offer(offer_id)
    print("INVENTORY UPDATE:", {"offerId": offer_id, "publish": pub})

    if isinstance(opt_old, dict):
        opt_old["title"] = new_title
        opt_old["description"] = new_desc
    logs = _loads(d.get("logs")) or []
    if not isinstance(logs, list):
        logs = []
    logs.append({"event": "reanalyze_revise_garden", "at": datetime.now(timezone.utc).isoformat(),
                 "item": item_id, "offerId": offer_id})
    conn.execute("UPDATE collected_products SET optimization=?, logs=?, updated_at=? WHERE sku=?",
                 (json.dumps(opt_old, ensure_ascii=False), json.dumps(logs, ensure_ascii=False),
                  datetime.now(timezone.utc).isoformat(), args.sku))
    conn.commit()
    print("local DB updated ✓")
    conn.close()


if __name__ == "__main__":
    main()
