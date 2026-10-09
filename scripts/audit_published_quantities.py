"""
Audit live eBay quantities for published listings and backfill stale qty=1 items
to the capped supplier quantity strategy.

Usage:
    python scripts/audit_published_quantities.py
    python scripts/audit_published_quantities.py --apply
    python scripts/audit_published_quantities.py --apply --only-qty-one
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import requests
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from scripts.sales_health_check import SalesHealthChecker
from src.clients.dajian_client import DaJianClient, extract_available_inventory_quantity
from src.plugins.inventory_sync.sync_service import InventorySyncService
from src.utils.ebay_quantity import assess_quantity_alignment
from src.services.ebay_auth import EbayOAuthService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("published_quantity_audit")


def _chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _load_published_rows(limit: int | None = None) -> list[dict]:
    conn = sqlite3.connect(str(PROJECT_ROOT / "ebay_collection.db"))
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT sku, title, listing_id
        FROM collected_products
        WHERE status = 'PUBLISHED'
        ORDER BY sku
    """
    params: tuple = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [
        {
            "sku": row["sku"],
            "title": row["title"] or "",
            "listing_id": row["listing_id"] or "",
        }
        for row in rows
    ]


def _build_dajian_client() -> DaJianClient:
    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    if not client_id or not client_secret:
        raise ValueError("Missing DAJIAN_API_KEY or DAJIAN_API_SECRET in .env")
    return DaJianClient(client_id, client_secret)


def _build_ebay_headers() -> dict[str, str]:
    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    token = oauth.get_valid_token()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }


def _select_offer(offers: list[dict]) -> dict | None:
    if not offers:
        return None
    return sorted(
        offers,
        key=lambda row: (
            0 if (row.get("listing") or {}).get("listingStatus") in ("ACTIVE", "OUT_OF_STOCK") else 1,
            0 if row.get("status") == "PUBLISHED" else 1,
            0 if row.get("marketplaceId") == "EBAY_US" else 1,
            row.get("offerId", ""),
        ),
    )[0]


def _fetch_inventory_item_quantity(sku: str, headers: dict[str, str]) -> int | None:
    try:
        resp = requests.get(
            f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}",
            headers=headers,
            timeout=30,
            verify=False,
        )
        if resp.status_code != 200:
            return None
        item = resp.json()
        return int(float(
            ((item.get("availability") or {}).get("shipToLocationAvailability") or {}).get("quantity")
        ))
    except Exception:
        return None


def _load_supplier_quantities(client: DaJianClient, skus: list[str]) -> dict[str, int | None]:
    quantity_map: dict[str, int | None] = {}
    for batch in _chunked(skus, 200):
        try:
            inventory_rows = client.get_inventory(batch)
        except Exception as exc:
            log.warning("Dajian inventory batch failed for %s items: %s", len(batch), exc)
            for sku in batch:
                quantity_map[sku] = None
            continue

        batch_map = {
            str(row.get("sku") or ""): extract_available_inventory_quantity(row)
            for row in inventory_rows or []
            if row.get("sku")
        }
        for sku in batch:
            quantity_map[sku] = batch_map.get(sku)
        time.sleep(0.4)
    return quantity_map


def _load_live_quantities_from_trading(rows: list[dict]) -> dict[str, int | None]:
    checker = SalesHealthChecker()
    trading_quantity_map = checker._fetch_trading_active_quantity_map()
    live_map: dict[str, int | None] = {}

    for row in rows:
        sku = row["sku"]
        listing_id = row["listing_id"]
        trading_state = trading_quantity_map.get(sku) or {}
        available = trading_state.get("available")
        if available is None and listing_id:
            available = checker._fetch_trading_listing_available_quantity(listing_id)
        live_map[sku] = available

    return live_map


def _load_live_quantities_from_offer(rows: list[dict]) -> dict[str, int | None]:
    headers = _build_ebay_headers()
    live_map: dict[str, int | None] = {}
    for index, row in enumerate(rows, start=1):
        sku = row["sku"]
        quantity = None
        try:
            resp = requests.get(
                f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}",
                headers=headers,
                timeout=30,
                verify=False,
            )
            if resp.status_code == 200:
                offer = _select_offer(resp.json().get("offers", []))
                if offer:
                    raw_quantity = offer.get("availableQuantity")
                    if raw_quantity is not None:
                        quantity = int(float(raw_quantity))
                    else:
                        quantity = _fetch_inventory_item_quantity(sku, headers)
        except Exception:
            quantity = None
        live_map[sku] = quantity
        if index % 50 == 0:
            time.sleep(0.5)
    return live_map


def _load_live_quantities(rows: list[dict], live_source: str) -> dict[str, int | None]:
    if live_source == "offer":
        return _load_live_quantities_from_offer(rows)
    return _load_live_quantities_from_trading(rows)


def _build_audit_entries(
    rows: list[dict],
    supplier_quantities: dict[str, int | None],
    live_quantities: dict[str, int | None],
) -> list[dict]:
    entries: list[dict] = []
    for row in rows:
        sku = row["sku"]
        decision = assess_quantity_alignment(
            live_quantities.get(sku),
            supplier_quantities.get(sku),
        )
        entries.append(
            {
                "sku": sku,
                "title": row["title"][:120],
                "listing_id": row["listing_id"],
                "live_quantity": decision.live_quantity,
                "supplier_quantity": decision.supplier_quantity,
                "desired_quantity": decision.desired_quantity,
                "status": decision.status,
                "should_update": decision.should_update,
            }
        )
    return entries


def _summarize(entries: list[dict]) -> dict:
    status_counter = Counter(entry["status"] for entry in entries)
    candidate_counter = Counter(
        entry["status"]
        for entry in entries
        if entry["should_update"] and entry["desired_quantity"] not in (None, 0)
    )
    out_of_stock_counter = Counter(
        entry["status"]
        for entry in entries
        if entry["should_update"] and entry["desired_quantity"] == 0
    )
    return {
        "total": len(entries),
        "status_counts": dict(status_counter),
        "fixable_positive_stock": dict(candidate_counter),
        "out_of_stock_candidates": dict(out_of_stock_counter),
    }


def _select_apply_candidates(
    entries: list[dict],
    only_qty_one: bool,
    include_out_of_stock: bool,
) -> list[dict]:
    selected = []
    for entry in entries:
        if not entry["should_update"]:
            continue
        if entry["desired_quantity"] is None:
            continue
        if entry["desired_quantity"] == 0 and not include_out_of_stock:
            continue
        if only_qty_one and entry["status"] != "stale_qty_one":
            continue
        selected.append(entry)
    return selected


def _apply_quantity_updates(entries: list[dict]) -> list[dict]:
    service = InventorySyncService()
    applied: list[dict] = []
    for index, entry in enumerate(entries, start=1):
        sku = entry["sku"]
        desired_quantity = entry["desired_quantity"]
        log.info(
            "[%s/%s] %s: live=%s supplier=%s target=%s status=%s",
            index,
            len(entries),
            sku,
            entry["live_quantity"],
            entry["supplier_quantity"],
            desired_quantity,
            entry["status"],
        )
        success = False
        error = None
        try:
            success = bool(service.update_ebay_quantity(sku, desired_quantity))
        except Exception as exc:
            error = str(exc)
        applied.append(
            {
                **entry,
                "apply_success": success,
                "apply_error": error,
            }
        )
        time.sleep(1.5)
    return applied


def _verify_entries(entries: list[dict]) -> list[dict]:
    if not entries:
        return []
    headers = _build_ebay_headers()
    results = []
    for entry in entries:
        live_quantity = None
        listing_status = None
        try:
            resp = requests.get(
                f"https://api.ebay.com/sell/inventory/v1/offer?sku={entry['sku']}",
                headers=headers,
                timeout=30,
                verify=False,
            )
            if resp.status_code == 200:
                offers = resp.json().get("offers", [])
                if offers:
                    offer = _select_offer(offers)
                    listing_status = (offer.get("listing") or {}).get("listingStatus")
                    raw_quantity = offer.get("availableQuantity")
                    if raw_quantity is not None:
                        try:
                            live_quantity = int(float(raw_quantity))
                        except (TypeError, ValueError):
                            live_quantity = None
                    if live_quantity is None:
                        live_quantity = _fetch_inventory_item_quantity(entry["sku"], headers)
        except Exception:
            live_quantity = None
        results.append(
            {
                "sku": entry["sku"],
                "listing_id": entry["listing_id"],
                "expected_quantity": entry["desired_quantity"],
                "live_quantity": live_quantity,
                "listing_status": listing_status,
                "verified": live_quantity == entry["desired_quantity"],
            }
        )
        time.sleep(0.5)
    return results


def _write_log(payload: dict) -> Path:
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"published_quantity_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit published listing quantities")
    parser.add_argument("--apply", action="store_true", help="Apply quantity fixes to eBay")
    parser.add_argument("--only-qty-one", action="store_true", help="Only fix stale live qty=1 listings")
    parser.add_argument(
        "--include-out-of-stock",
        action="store_true",
        help="Also set eBay quantity to 0 when supplier quantity is 0",
    )
    parser.add_argument(
        "--live-source",
        choices=("trading", "offer"),
        default="trading",
        help="Source used to read current eBay live quantity",
    )
    parser.add_argument("--max-apply", type=int, help="Limit number of apply candidates in this run")
    parser.add_argument("--limit", type=int, help="Limit published rows for debugging")
    args = parser.parse_args()

    rows = _load_published_rows(limit=args.limit)
    log.info("Loaded %s published listings", len(rows))
    if not rows:
        log.info("No published listings found")
        return 0

    dajian = _build_dajian_client()
    skus = [row["sku"] for row in rows]
    supplier_quantities = _load_supplier_quantities(dajian, skus)
    live_quantities = _load_live_quantities(rows, args.live_source)
    entries = _build_audit_entries(rows, supplier_quantities, live_quantities)
    summary = _summarize(entries)

    log.info("Status counts: %s", summary["status_counts"])
    log.info("Positive-stock fix candidates: %s", summary["fixable_positive_stock"])
    log.info("Out-of-stock candidates (report only): %s", summary["out_of_stock_candidates"])

    apply_candidates = _select_apply_candidates(
        entries,
        only_qty_one=args.only_qty_one,
        include_out_of_stock=args.include_out_of_stock,
    )
    if args.max_apply and args.max_apply > 0:
        apply_candidates = apply_candidates[:args.max_apply]
    log.info("Apply candidates: %s", len(apply_candidates))

    payload = {
        "timestamp": datetime.now().isoformat(),
        "mode": "apply" if args.apply else "dry-run",
        "only_qty_one": bool(args.only_qty_one),
        "include_out_of_stock": bool(args.include_out_of_stock),
        "live_source": args.live_source,
        "max_apply": args.max_apply,
        "summary": summary,
        "apply_candidate_count": len(apply_candidates),
        "apply_candidates": apply_candidates,
        "out_of_stock_candidates": [
            entry for entry in entries
            if entry["should_update"] and entry["desired_quantity"] == 0
        ],
    }

    if args.apply and apply_candidates:
        applied = _apply_quantity_updates(apply_candidates)
        verify_rows = _verify_entries([row for row in applied if row["apply_success"]])
        payload["applied"] = applied
        payload["verification"] = verify_rows
        payload["apply_success_count"] = sum(1 for row in applied if row["apply_success"])
        payload["apply_failure_count"] = sum(1 for row in applied if not row["apply_success"])
        payload["verification_success_count"] = sum(1 for row in verify_rows if row["verified"])
        payload["verification_failure_count"] = sum(1 for row in verify_rows if not row["verified"])

    log_path = _write_log(payload)
    log.info("Audit log written to %s", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
