"""Phase 2: poll GIGA status/tracking and write back eBay shipping fulfillments.

Flow for each local row with status=pushed (or dry_run_ready for status-only):
  1) query_order_status(giga_order_no)
  2) query_order_tracking(giga_order_no)
  3) if tracking present → POST eBay createShippingFulfillment
  4) update giga_fulfillment_orders.status → shipped / still_processing / error
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "ebay_collection.db"

# Map common GIGA carrier names → eBay shippingCarrierCode values
_CARRIER_MAP = {
    "fedex": "FedEx",
    "federal express": "FedEx",
    "ups": "UPS",
    "united parcel service": "UPS",
    "usps": "USPS",
    "united states postal service": "USPS",
    "dhl": "DHL",
    "dhl express": "DHL",
    "ontrac": "OnTrac",
    "on trac": "OnTrac",
    "amazon logistics": "Amazon",
    "amazon shipping": "Amazon",
    "gofo": "Other",
    "gofo express": "Other",
    "gofo ground": "Other",
    "uniuni": "Other",
}


def map_carrier_code(carrier_name: str | None) -> str:
    if not carrier_name:
        return "Other"
    key = str(carrier_name).strip().lower()
    if key in _CARRIER_MAP:
        return _CARRIER_MAP[key]
    # partial contains
    for needle, code in _CARRIER_MAP.items():
        if needle in key:
            return code
    return "Other"


def extract_tracking_entries(track_rows: list[dict]) -> list[dict]:
    """Flatten GIGA track-no response into [{trackingNum, carrierName, sku, skuQty}]."""
    out: list[dict] = []
    for row in track_rows or []:
        for ship in row.get("shipTrackInfo") or []:
            num = str(ship.get("trackingNum") or "").strip()
            if not num:
                continue
            out.append(
                {
                    "trackingNum": num,
                    "carrierName": ship.get("carrierName") or "",
                    "sku": ship.get("sku") or "",
                    "skuQty": ship.get("skuQty"),
                    "warehouseCode": ((ship.get("shipFromInfo") or {}).get("warehouseCode")),
                    "orderNo": row.get("orderNo"),
                }
            )
    return out


def is_terminal_giga_status(order_status: str | None) -> bool:
    s = str(order_status or "").strip()
    # Completed / cancelled-ish — treat completed as shippable terminal
    return s in ("20", "10", "210", "Completed", "Cancelled")


def is_shipped_giga_status(order_status: str | None) -> bool:
    s = str(order_status or "").strip()
    return s in ("20", "Completed", "100", "150", "207", "208") or s == "20"


def list_pending_fulfillment_rows(
    conn: sqlite3.Connection,
    *,
    statuses: tuple[str, ...] = (
        "pushed",
        "still_processing",
        "fulfill_failed",
        "sync_error",
    ),
    ebay_order_id: Optional[str] = None,
) -> list[dict]:
    from src.services.giga_dropship import ensure_fulfillment_table

    ensure_fulfillment_table(conn)
    conn.row_factory = sqlite3.Row
    if ebay_order_id:
        rows = conn.execute(
            "SELECT * FROM giga_fulfillment_orders WHERE ebay_order_id = ?",
            (ebay_order_id,),
        ).fetchall()
    else:
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM giga_fulfillment_orders WHERE status IN ({placeholders}) "
            f"ORDER BY updated_at ASC",
            statuses,
        ).fetchall()
    return [dict(r) for r in rows]


def get_ebay_order(order_id: str, *, oauth=None) -> dict:
    from src.services.ebay_auth import EbayOAuthService

    if oauth is None:
        oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    token = oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    r = requests.get(
        f"https://api.ebay.com/sell/fulfillment/v1/order/{order_id}",
        headers=headers,
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"getOrder {order_id} failed {r.status_code}: {r.text[:400]}")
    return r.json()


def create_ebay_shipping_fulfillment(
    order_id: str,
    *,
    line_items: list[dict],
    tracking_number: str,
    shipping_carrier_code: str,
    shipped_date: Optional[str] = None,
    oauth=None,
    dry_run: bool = False,
) -> dict:
    """POST /sell/fulfillment/v1/order/{orderId}/shipping_fulfillment"""
    from src.services.ebay_auth import EbayOAuthService

    if not line_items:
        raise ValueError("line_items required")
    if not tracking_number:
        raise ValueError("tracking_number required")

    if shipped_date is None:
        shipped_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body = {
        "lineItems": line_items,
        "shippedDate": shipped_date,
        "shippingCarrierCode": shipping_carrier_code or "Other",
        "trackingNumber": tracking_number,
    }
    if dry_run:
        return {
            "dry_run": True,
            "order_id": order_id,
            "endpoint": f"/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment",
            "body": body,
        }

    if oauth is None:
        oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    token = oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    r = requests.post(
        f"https://api.ebay.com/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment",
        headers=headers,
        json=body,
        timeout=60,
    )
    # 201 Created is success; body often empty
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"createShippingFulfillment {order_id} failed {r.status_code}: {r.text[:500]}"
        )
    try:
        data = r.json() if r.content else {}
    except Exception:
        data = {}
    return {
        "dry_run": False,
        "order_id": order_id,
        "status_code": r.status_code,
        "response": data,
        "body": body,
    }


def _line_items_for_tracking(order: dict, track_entries: list[dict]) -> list[dict]:
    """Match eBay line items to tracking; if single package, ship all NOT_STARTED lines."""
    ebay_lines = order.get("lineItems") or []
    open_lines = [
        li
        for li in ebay_lines
        if str(li.get("lineItemFulfillmentStatus") or "").upper()
        in ("", "NOT_STARTED", "IN_PROGRESS")
    ]
    if not open_lines:
        open_lines = list(ebay_lines)

    # Prefer SKU match when track has sku
    track_skus = {str(t.get("sku") or "").strip() for t in track_entries if t.get("sku")}
    tracked_qty_by_sku: dict[str, int] = {}
    for track in track_entries:
        sku = str(track.get("sku") or "").strip()
        if not sku:
            continue
        try:
            qty = int(track.get("skuQty"))
        except (TypeError, ValueError):
            continue
        if qty > 0:
            tracked_qty_by_sku[sku] = tracked_qty_by_sku.get(sku, 0) + qty
    matched = []
    for li in open_lines:
        sku = str(li.get("sku") or "").strip()
        if track_skus and sku and sku not in track_skus:
            # still include if only one open line
            if len(open_lines) > 1:
                continue
        lid = li.get("lineItemId")
        if not lid:
            continue
        ordered_qty = int(li.get("quantity") or 1)
        qty = min(ordered_qty, tracked_qty_by_sku.get(sku, ordered_qty))
        matched.append({"lineItemId": str(lid), "quantity": qty})

    if not matched and len(open_lines) == 1:
        # A single open line is unambiguous even when GIGA omitted its SKU.
        matched = [
            {"lineItemId": str(li["lineItemId"]), "quantity": int(li.get("quantity") or 1)}
            for li in open_lines
            if li.get("lineItemId")
        ]
    return matched


def sync_one_fulfillment_row(
    row: dict,
    *,
    dajian_client,
    oauth=None,
    dry_run: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Poll GIGA + optionally write eBay tracking for one local fulfillment row."""
    from src.services.giga_dropship import record_fulfillment_attempt

    ebay_id = row["ebay_order_id"]
    giga_no = row["giga_order_no"]
    result: dict[str, Any] = {
        "ebay_order_id": ebay_id,
        "giga_order_no": giga_no,
        "dry_run": dry_run,
    }

    # 1) GIGA status
    try:
        status_rows = dajian_client.query_order_status([giga_no])
    except Exception as e:
        result["status"] = "giga_status_error"
        result["error"] = str(e)
        if conn is not None and not dry_run:
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="sync_error",
                error=str(e),
            )
        return result

    st = (status_rows or [{}])[0] if status_rows else {}
    giga_status = st.get("orderStatus")
    can_cancel = st.get("canCancel")
    result["giga_status"] = giga_status
    result["can_cancel"] = can_cancel

    # 2) GIGA tracking
    try:
        track_rows = dajian_client.query_order_tracking([giga_no])
    except Exception as e:
        result["status"] = "giga_track_error"
        result["error"] = str(e)
        if conn is not None and not dry_run:
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="still_processing",
                error=f"track error: {e}",
            )
        return result

    tracks = extract_tracking_entries(track_rows if isinstance(track_rows, list) else [])
    result["tracking"] = tracks

    if not tracks:
        result["status"] = "still_processing"
        result["message"] = f"no tracking yet (giga_status={giga_status})"
        if conn is not None and not dry_run:
            # Persist GIGA orderStatus so finance F1 can show 已推未付 etc.
            now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            conn.execute(
                """
                UPDATE giga_fulfillment_orders
                SET tracking_json = ?, status = ?, last_error = ?, updated_at = ?
                WHERE ebay_order_id = ?
                """,
                (
                    json.dumps(
                        {"giga_status": giga_status, "can_cancel": can_cancel, "tracking": []},
                        ensure_ascii=False,
                    ),
                    "still_processing",
                    result["message"],
                    now,
                    ebay_id,
                ),
            )
            conn.commit()
        return result

    tracking_numbers = {str(track.get("trackingNum") or "").strip() for track in tracks}
    if len(tracking_numbers) > 1:
        result["status"] = "multiple_tracking_numbers_unsupported"
        result["error"] = "multiple GIGA tracking numbers require manual review"
        if conn is not None and not dry_run:
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="manual_review",
                error=result["error"],
            )
        return result

    # Use first tracking number for the package (multi-package: first for now)
    primary = tracks[0]
    carrier = map_carrier_code(primary.get("carrierName"))
    tracking_number = primary["trackingNum"]

    # 3) eBay order line items
    try:
        order = get_ebay_order(ebay_id, oauth=oauth)
    except Exception as e:
        result["status"] = "ebay_order_error"
        result["error"] = str(e)
        return result

    fulfill_status = str(order.get("orderFulfillmentStatus") or "").upper()
    if fulfill_status == "FULFILLED":
        result["status"] = "already_fulfilled_on_ebay"
        if conn is not None and not dry_run:
            payload = None
            if row.get("payload_json"):
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    payload = None
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="shipped",
                payload=payload,
            )
        return result

    line_items = _line_items_for_tracking(order, tracks)
    if not line_items:
        result["status"] = "no_line_items"
        result["error"] = "could not map eBay lineItemId for fulfillment"
        return result

    # 4) create shipping fulfillment
    try:
        ship_resp = create_ebay_shipping_fulfillment(
            ebay_id,
            line_items=line_items,
            tracking_number=tracking_number,
            shipping_carrier_code=carrier,
            oauth=oauth,
            dry_run=dry_run,
        )
    except Exception as e:
        result["status"] = "ebay_fulfill_error"
        result["error"] = str(e)
        if conn is not None and not dry_run:
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="fulfill_failed",
                error=str(e),
            )
        return result

    result["ebay_fulfill"] = ship_resp
    result["status"] = "shipped_dry_run" if dry_run else "shipped"

    if conn is not None:
        # persist tracking + giga status for finance fulfill stage
        conn.execute(
            """
            UPDATE giga_fulfillment_orders
            SET tracking_json = ?, updated_at = ?
            WHERE ebay_order_id = ?
            """,
            (
                json.dumps(
                    {
                        "giga_status": giga_status,
                        "can_cancel": can_cancel,
                        "tracking": tracks,
                    },
                    ensure_ascii=False,
                ),
                datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
                ebay_id,
            ),
        )
        conn.commit()
        if not dry_run:
            payload = None
            if row.get("payload_json"):
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    payload = None
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="shipped",
                payload=payload,
            )

    return result


def sync_pending_fulfillments(
    *,
    db_path: str | Path = DEFAULT_DB,
    dry_run: bool = True,
    ebay_order_id: Optional[str] = None,
    dajian_client=None,
    oauth=None,
) -> list[dict]:
    """Run Phase 2 sync for pending local fulfillment rows."""
    from src.clients.dajian_client import DaJianClient
    from src.services.ebay_auth import EbayOAuthService

    if dajian_client is None:
        key = os.getenv("DAJIAN_API_KEY") or os.getenv("DAJIAN_CLIENT_ID")
        secret = os.getenv("DAJIAN_API_SECRET") or os.getenv("DAJIAN_CLIENT_SECRET")
        if not key or not secret:
            raise RuntimeError("DAJIAN_API_KEY / DAJIAN_API_SECRET required for GIGA poll")
        dajian_client = DaJianClient(key, secret)
    if oauth is None:
        oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))

    conn = sqlite3.connect(str(db_path))
    rows = list_pending_fulfillment_rows(conn, ebay_order_id=ebay_order_id)
    # Also allow dry_run_ready rows for status probe when explicitly requested
    if ebay_order_id and not rows:
        rows = list_pending_fulfillment_rows(
            conn,
            statuses=(
                "pushed",
                "still_processing",
                "dry_run_ready",
                "fulfill_failed",
                "sync_error",
                "manual_review",
            ),
            ebay_order_id=ebay_order_id,
        )

    results = []
    for row in rows:
        # Only write back when already pushed (or retry fulfill_failed / still_processing)
        if dry_run is False and row.get("status") == "dry_run_ready":
            results.append(
                {
                    "ebay_order_id": row["ebay_order_id"],
                    "status": "skipped_not_pushed",
                    "message": "row is dry_run_ready; push to GIGA first with --apply",
                }
            )
            continue
        results.append(
            sync_one_fulfillment_row(
                row,
                dajian_client=dajian_client,
                oauth=oauth,
                dry_run=dry_run,
                conn=conn,
            )
        )
    conn.close()
    return results
