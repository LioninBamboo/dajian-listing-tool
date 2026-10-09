#!/usr/bin/env python3
"""Generate a read-only source/GIGA/eBay reconciliation report."""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.services.listing_qc_cross_system_report import build_cross_system_report  # noqa: E402


def _load_scope_files(paths: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        for line in Path(raw_path).read_text(encoding="utf-8").splitlines():
            sku = line.strip()
            if not sku or sku.startswith("#") or sku in seen:
                continue
            seen.add(sku)
            values.append(sku)
    return values


def _load_products(conn: sqlite3.Connection, skus: list[str], statuses: list[str]) -> list[dict]:
    if skus:
        placeholders = ",".join("?" for _ in skus)
        rows = conn.execute(
            f"SELECT * FROM collected_products WHERE sku IN ({placeholders})",
            tuple(skus),
        ).fetchall()
        by_sku = {str(row["sku"]): dict(row) for row in rows}
        return [by_sku[sku] for sku in skus if sku in by_sku]
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM collected_products WHERE status IN ({placeholders}) ORDER BY updated_at DESC",
        tuple(statuses),
    ).fetchall()
    return [dict(row) for row in rows]


def _fetch_live_giga(skus: list[str]) -> tuple[dict, dict]:
    from src.clients.dajian_client import DaJianClient

    key = os.getenv("DAJIAN_API_KEY")
    secret = os.getenv("DAJIAN_API_SECRET")
    if not key or not secret:
        return {}, {sku: "DAJIAN_API_KEY/DAJIAN_API_SECRET missing" for sku in skus}
    try:
        client = DaJianClient(key, secret)
        rows = client.get_inventory(skus)
    except Exception as exc:
        return {}, {sku: str(exc) for sku in skus}
    by_sku = {str(row.get("sku")): row for row in rows if isinstance(row, dict) and row.get("sku")}
    errors = {sku: "SKU missing from GIGA inventory response" for sku in skus if sku not in by_sku}
    return by_sku, errors


def _fetch_live_ebay(products: list[dict]) -> tuple[dict, dict, dict, dict]:
    live_products = [
        product for product in products
        if str(product.get("status") or "").upper() == "PUBLISHED" or product.get("listing_id")
    ]
    if not live_products:
        return {}, {}, {}, {}
    try:
        from src.clients.real_ebay_client import create_real_ebay_client

        client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        if not client.oauth.is_authorized():
            message = "eBay OAuth is not authorized"
            return {}, {}, {}, {str(product["sku"]): message for product in live_products}
    except Exception as exc:
        return {}, {}, {}, {str(product["sku"]): str(exc) for product in live_products}

    inventories: dict[str, dict] = {}
    offers: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for product in live_products:
        sku = str(product["sku"])
        try:
            inventory = client.get_inventory_item(sku) or {}
            offer_rows = client.get_offers_by_sku(sku) or []
            if inventory:
                inventories[sku] = inventory
            if offer_rows:
                expected_listing_id = str(product.get("listing_id") or "")
                selected = next(
                    (
                        offer for offer in offer_rows
                        if expected_listing_id and str((offer.get("listing") or {}).get("listingId") or offer.get("listingId") or "") == expected_listing_id
                    ),
                    offer_rows[0],
                )
                offers[sku] = selected
            if not inventory:
                errors[sku] = "eBay inventory item not found"
        except Exception as exc:
            errors[sku] = str(exc)
    return inventories, offers, errors, {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sku-file", action="append", default=[], help="SKU file; repeatable")
    parser.add_argument("--status", action="append", default=["READY", "READY_TO_PUBLISH", "ERROR", "PUBLISHED"])
    parser.add_argument("--live", action="store_true", help="Read GIGA/eBay APIs; never writes")
    parser.add_argument("--db", default=str(ROOT / "ebay_collection.db"))
    parser.add_argument("--report", help="Output JSON report path")
    args = parser.parse_args()

    scope = _load_scope_files(args.sku_file) if args.sku_file else []
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        products = _load_products(conn, scope, args.status)
    finally:
        conn.close()

    giga, giga_errors = ({}, {})
    ebay_inventory, ebay_offers, ebay_errors, _ = ({}, {}, {}, {})
    if args.live and products:
        skus = [str(product["sku"]) for product in products]
        giga, giga_errors = _fetch_live_giga(skus)
        ebay_inventory, ebay_offers, ebay_errors, _ = _fetch_live_ebay(products)

    report = build_cross_system_report(
        products,
        giga_inventory_by_sku=giga,
        ebay_inventory_by_sku=ebay_inventory,
        ebay_offer_by_sku=ebay_offers,
        giga_errors=giga_errors,
        ebay_errors=ebay_errors,
        metadata={
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db": str(Path(args.db).resolve()),
            "live_reads": bool(args.live),
            "scope_files": args.sku_file,
            "giga_error_count": len(giga_errors),
            "ebay_error_count": len(ebay_errors),
        },
    )
    path = Path(args.report) if args.report else ROOT / "logs" / f"listing_qc_cross_system_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

