#!/usr/bin/env python3
"""Remove supplier-brand prefixes from live eBay listing titles."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from src.clients.ebay_client import EbayClient
from src.clients.ebay_trading_client import EbayTradingClient
from src.services.ebay_auth import EbayOAuthService
from src.utils.title_sanitizer import normalize_listing_title_for_ebay, strip_supplier_brand_prefix


LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"supplier_brand_cleanup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def _load_candidates(conn: sqlite3.Connection, only_sku: str | None = None) -> list[dict]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if only_sku:
        rows = cur.execute(
            """
            SELECT sku, title, optimization, listing_id, status
            FROM collected_products
            WHERE sku = ?
            """,
            (only_sku,),
        ).fetchall()
    else:
        rows = cur.execute(
            """
            SELECT sku, title, optimization, listing_id, status
            FROM collected_products
            WHERE status = 'PUBLISHED'
              AND listing_id IS NOT NULL
              AND listing_id != ''
            ORDER BY sku
            """
        ).fetchall()

    candidates = []
    for row in rows:
        optimization = {}
        if row["optimization"]:
            try:
                optimization = json.loads(row["optimization"])
            except Exception:
                optimization = {}

        source_title = (optimization.get("title") or row["title"] or "").strip()
        cleaned, changed, removed_prefix = strip_supplier_brand_prefix(source_title)
        if not changed:
            continue

        safe_title, _ = normalize_listing_title_for_ebay(cleaned, source_title=source_title)
        candidates.append(
            {
                "sku": row["sku"],
                "status": row["status"],
                "listing_id": str(row["listing_id"]),
                "old_title": source_title,
                "new_title": safe_title,
                "removed_prefix": removed_prefix,
                "optimization": optimization,
            }
        )
    return candidates


def _build_clients() -> tuple[EbayOAuthService, EbayTradingClient]:
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper()
    oauth = EbayOAuthService(environment)
    if not oauth.is_authorized():
        raise RuntimeError("eBay OAuth is not authorized")

    ebay = EbayClient(
        os.getenv("EBAY_APP_ID"),
        os.getenv("EBAY_CERT_ID"),
        os.getenv("EBAY_DEV_ID"),
        env="production" if environment == "PRODUCTION" else "sandbox",
    )
    trading = EbayTradingClient(ebay)
    return oauth, trading


def _revise_title_via_inventory_api(
    oauth: EbayOAuthService,
    sku: str,
    listing_id: str,
    new_title: str,
) -> tuple[bool, str]:
    token = oauth.get_valid_token()
    base_url = oauth.api_base
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }

    inv_url = f"{base_url}/sell/inventory/v1/inventory_item/{sku}"
    inv_resp = requests.get(inv_url, headers=headers, timeout=40, verify=False)
    if inv_resp.status_code != 200:
        return False, f"inventory_get_{inv_resp.status_code}"

    inv_data = inv_resp.json()
    inv_data.pop("sku", None)
    inv_data.pop("locale", None)

    pws = inv_data.get("packageWeightAndSize", {}) or {}
    weight = (pws.get("weight") or {}).get("value")
    try:
        if weight is not None and float(weight) <= 0:
            inv_data.pop("packageWeightAndSize", None)
    except Exception:
        inv_data.pop("packageWeightAndSize", None)

    availability = inv_data.get("availability", {}) or {}
    ship_to = availability.get("shipToLocationAvailability", {}) or {}
    ship_to.pop("allocationByFormat", None)

    inv_data.setdefault("product", {})
    safe_title, _ = normalize_listing_title_for_ebay(new_title, source_title=new_title)
    inv_data["product"]["title"] = safe_title

    put_resp = requests.put(inv_url, headers=headers, json=inv_data, timeout=60, verify=False)
    if put_resp.status_code not in (200, 204):
        return False, f"inventory_put_{put_resp.status_code}"

    offers_url = f"{base_url}/sell/inventory/v1/offer"
    offers_resp = requests.get(offers_url, headers=headers, params={"sku": sku}, timeout=40, verify=False)
    if offers_resp.status_code != 200:
        return False, f"offer_get_{offers_resp.status_code}"

    offers = offers_resp.json().get("offers", []) or []
    if not offers:
        return False, "offer_not_found"

    best_offer = None
    for offer in offers:
        listing_meta = offer.get("listing") or {}
        if str(listing_meta.get("listingId") or "") == str(listing_id):
            best_offer = offer
            break
    if not best_offer:
        best_offer = offers[0]

    offer_id = best_offer.get("offerId")
    if not offer_id:
        return False, "offer_id_missing"

    pub_url = f"{base_url}/sell/inventory/v1/offer/{offer_id}/publish"
    pub_resp = requests.post(pub_url, headers=headers, timeout=60, verify=False)
    if pub_resp.status_code != 200:
        return False, f"offer_publish_{pub_resp.status_code}"

    return True, "inventory_api"


def _revise_title(
    trading: EbayTradingClient,
    oauth: EbayOAuthService,
    sku: str,
    listing_id: str,
    new_title: str,
) -> tuple[bool, str]:
    xml_payload = f"""
    <Item>
        <ItemID>{listing_id}</ItemID>
        <Title>{xml_escape(new_title)}</Title>
    </Item>
    """
    try:
        response = trading.call("ReviseItem", xml_payload)
        root = ET.fromstring(response)
        ns = {"ebay": "urn:ebay:apis:eBLBaseComponents"}
        ack = root.find(".//ebay:Ack", ns)
        if ack is not None and ack.text in {"Success", "Warning"}:
            return True, "trading_api"

        errors = root.findall(".//ebay:Errors", ns)
        for err in errors:
            code = err.find("ebay:ErrorCode", ns)
            if code is None:
                continue
            if code.text in {"21919474", "21919456"}:
                return _revise_title_via_inventory_api(oauth, sku, listing_id, new_title)
            if code.text == "291":
                return False, "listing_ended"
        return False, "trading_api_error"
    except Exception as e:
        return False, f"exception_{str(e)[:80]}"


def _update_local_db(conn: sqlite3.Connection, row: dict, channel: str) -> None:
    cur = conn.cursor()
    optimization = row["optimization"] if isinstance(row["optimization"], dict) else {}
    optimization["title"] = row["new_title"]

    db_row = cur.execute(
        "SELECT logs FROM collected_products WHERE sku = ?",
        (row["sku"],),
    ).fetchone()
    logs = []
    if db_row and db_row[0]:
        try:
            logs = json.loads(db_row[0]) if isinstance(db_row[0], str) else (db_row[0] or [])
        except Exception:
            logs = []
    logs.append(
        f"Removed supplier title prefix '{row['removed_prefix']}' via {channel} at {datetime.now(timezone.utc).isoformat()}"
    )

    cur.execute(
        """
        UPDATE collected_products
        SET title = ?, optimization = ?, logs = ?, updated_at = ?
        WHERE sku = ?
        """,
        (
            row["new_title"],
            json.dumps(optimization, ensure_ascii=False),
            json.dumps(logs, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
            row["sku"],
        ),
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove supplier brand prefixes from live listing titles.")
    parser.add_argument("--sku", help="Only process one SKU")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, do not call eBay APIs")
    parser.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = no limit)")
    args = parser.parse_args()

    conn = sqlite3.connect(str(PROJECT_ROOT / "ebay_collection.db"))
    try:
        rows = _load_candidates(conn, only_sku=args.sku)
        if args.limit and args.limit > 0:
            rows = rows[: args.limit]

        if not rows:
            logger.info("No published listings need supplier-prefix cleanup.")
            return 0

        logger.info("Found %s listings with supplier-brand prefixes.", len(rows))
        for row in rows[:10]:
            logger.info("  %s: %s -> %s", row["sku"], row["old_title"][:70], row["new_title"][:70])

        if args.dry_run:
            logger.info("Dry-run mode; no eBay updates executed.")
            return 0

        oauth, trading = _build_clients()
        results = []
        success = 0
        failed = 0

        for row in rows:
            ok, channel = _revise_title(
                trading=trading,
                oauth=oauth,
                sku=row["sku"],
                listing_id=row["listing_id"],
                new_title=row["new_title"],
            )
            result = {
                "sku": row["sku"],
                "listing_id": row["listing_id"],
                "old_title": row["old_title"],
                "new_title": row["new_title"],
                "removed_prefix": row["removed_prefix"],
                "status": "success" if ok else "error",
                "channel": channel,
            }
            results.append(result)

            if ok:
                success += 1
                _update_local_db(conn, row, channel)
                logger.info("✅ %s updated via %s", row["sku"], channel)
            else:
                failed += 1
                logger.warning("❌ %s failed (%s)", row["sku"], channel)

            time.sleep(1.0)

        report_path = LOG_DIR / f"supplier_brand_cleanup_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total": len(rows),
            "success": success,
            "failed": failed,
            "results": results,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Done. success=%s failed=%s report=%s", success, failed, report_path)
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
