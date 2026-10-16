#!/usr/bin/env python
"""GIGA fulfillment Phase 0 — dry-run / read-only queries.

Examples:
  # Live read (needs DAJIAN_API_KEY / DAJIAN_API_SECRET)
  python scripts/giga_fulfillment_dry_run.py warehouse --codes CA3,TX1
  python scripts/giga_fulfillment_dry_run.py status --orders DSR123,DSR456
  python scripts/giga_fulfillment_dry_run.py track --orders DSR123

  # Dropship payload dry-run (no API write; GIGA chooses warehouse)
  python scripts/giga_fulfillment_dry_run.py dropship-sample --out reports/dropship_sample.json

Docs: docs/GIGA_FULFILLMENT_API.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")


def _client():
    from src.clients.dajian_client import DaJianClient

    key = os.getenv("DAJIAN_API_KEY") or os.getenv("DAJIAN_CLIENT_ID")
    secret = os.getenv("DAJIAN_API_SECRET") or os.getenv("DAJIAN_CLIENT_SECRET")
    if not key or not secret:
        raise SystemExit("Missing DAJIAN_API_KEY / DAJIAN_API_SECRET in env")
    return DaJianClient(key, secret)


def _split_csv(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def cmd_warehouse(args):
    codes = _split_csv(args.codes)
    client = _client()
    data = client.query_warehouse_addresses(codes)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_status(args):
    orders = _split_csv(args.orders)
    client = _client()
    data = client.query_order_status(orders)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_track(args):
    orders = _split_csv(args.orders)
    client = _client()
    data = client.query_order_tracking(orders)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_dropship_sample(args):
    """Build a sample eBay-style dropship payload and dry-run validate (no write)."""
    from src.clients.dajian_client import DaJianClient

    client = DaJianClient("dry-run", "dry-run")
    payload = {
        "orderDate": args.order_date,
        "orderNo": args.order_no,
        "shipName": args.ship_name,
        "shipPhone": args.ship_phone,
        "shipEmail": args.ship_email or "",
        "shipAddress1": args.ship_address1,
        "shipAddress2": args.ship_address2 or "",
        "shipCity": args.ship_city,
        "shipCountry": args.ship_country,
        "shipState": args.ship_state or "",
        "shipZipCode": args.ship_zip,
        "salesChannel": "eBay",
        "ebayTransactionID": args.ebay_transaction_id or "",
        "orderLines": [
            {
                "itemPrice": float(args.item_price),
                "qty": int(args.qty),
                "sku": args.sku,
                "productName": args.product_name or args.sku,
                "ebayItemCode": args.ebay_item_code or "",
                "currencyCode": "USD",
            }
        ],
        "orderTotal": float(args.item_price) * int(args.qty),
        "valueAddedServices": {
            "returnLabelService": False,
            "deliveryService": "DSR",
        },
        # NOTE: no warehouseCode — GIGA selects ship-from warehouse for dropship.
    }
    result = client.import_dropship_order(payload, dry_run=True)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"\nWrote {out}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="GIGA fulfillment Phase 0 dry-run / read queries")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_wh = sub.add_parser("warehouse", help="Query warehouse addresses (live API)")
    p_wh.add_argument("--codes", required=True, help="Comma-separated warehouse codes")
    p_wh.set_defaults(func=cmd_warehouse)

    p_st = sub.add_parser("status", help="Query order status (live API)")
    p_st.add_argument("--orders", required=True, help="Comma-separated GIGA orderNo list")
    p_st.set_defaults(func=cmd_status)

    p_tr = sub.add_parser("track", help="Query shipping tracking (live API)")
    p_tr.add_argument("--orders", required=True, help="Comma-separated GIGA orderNo list")
    p_tr.set_defaults(func=cmd_track)

    p_ds = sub.add_parser(
        "dropship-sample",
        help="Validate sample dropship payload only (no API write; no warehouse needed)",
    )
    p_ds.add_argument("--order-no", default="99-00000-00001")
    p_ds.add_argument("--order-date", default="2026-08-11 12:00:00")
    p_ds.add_argument("--sku", default="W3118P505149")
    p_ds.add_argument("--qty", default="1")
    p_ds.add_argument("--item-price", default="227.16")
    p_ds.add_argument("--product-name", default="")
    p_ds.add_argument("--ship-name", default="Jane Doe")
    p_ds.add_argument("--ship-phone", default="5551234567")
    p_ds.add_argument("--ship-email", default="")
    p_ds.add_argument("--ship-address1", default="123 Main St")
    p_ds.add_argument("--ship-address2", default="")
    p_ds.add_argument("--ship-city", default="Los Angeles")
    p_ds.add_argument("--ship-state", default="CA")
    p_ds.add_argument("--ship-country", default="US")
    p_ds.add_argument("--ship-zip", default="90001")
    p_ds.add_argument("--ebay-transaction-id", default="")
    p_ds.add_argument("--ebay-item-code", default="")
    p_ds.add_argument("--out", default="")
    p_ds.set_defaults(func=cmd_dropship_sample)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
