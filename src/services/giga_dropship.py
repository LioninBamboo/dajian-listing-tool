"""eBay order → GIGA one-piece dropship payload builder (Phase 1).

Dropship does **not** require a warehouseCode — GIGA selects ship-from.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "ebay_collection.db"

# Statuses that are safe to re-plan / re-reserve. Excludes push_unknown
# (acceptance is ambiguous after timeout) and pushed/in-progress states.
RETRYABLE_FULFILLMENT_STATUSES = (
    "dry_run_ready",
    "validation_failed",
    "stock_blocked",
    "stock_check_failed",
    "push_failed",
)


def is_ambiguous_dropship_push_error(exc: BaseException) -> bool:
    """True when GIGA may have accepted the order despite the exception.

    Timeouts, connection/proxy failures, and HTTP 5xx after POST must stay
    ``push_unknown`` (duplicate-order risk). HTTP 4xx, GIGA business errors,
    and local validation failures are definite rejections.
    """
    import requests

    if isinstance(exc, (TimeoutError, requests.exceptions.Timeout)):
        return True
    if isinstance(
        exc,
        (requests.exceptions.ConnectionError, requests.exceptions.ProxyError),
    ):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        try:
            return int(status) >= 500
        except (TypeError, ValueError):
            return True
    msg = str(exc or "").lower()
    return any(
        marker in msg
        for marker in ("timeout", "timed out", "connection error", "proxy error")
    )

# eBay order id may contain hyphens; GIGA orderNo only allows letters/digits/._-
_ORDER_NO_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def make_giga_order_no(ebay_order_id: str, *, prefix: str = "") -> str:
    """Build GIGA ``orderNo`` from eBay Order Number.

    Uses the eBay order id as-is (sanitized only if illegal chars appear).
    No forced ``EB`` prefix — GIGA eBay template ``*OrderId`` is the plain
    eBay Order Number (e.g. ``13-15010-93245``).
    """
    raw = str(ebay_order_id or "").strip()
    # eBay order numbers like 13-15010-93245 are already legal for GIGA.
    if raw and all(c.isalnum() or c in "._-" for c in raw):
        return raw[-64:] if len(raw) > 64 else raw
    cleaned = _ORDER_NO_SAFE.sub("_", raw).strip("._-")
    if not cleaned:
        cleaned = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    body = cleaned[-64:] if len(cleaned) > 64 else cleaned
    if prefix:
        return f"{prefix}{body}"
    return body


def _money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ship_to_from_order(order: dict) -> dict:
    """Extract ship-to address from Fulfillment order JSON."""
    instructions = order.get("fulfillmentStartInstructions") or []
    for inst in instructions:
        step = (inst or {}).get("shippingStep") or {}
        ship_to = step.get("shipTo") or {}
        if ship_to:
            addr = ship_to.get("contactAddress") or {}
            phone = (ship_to.get("primaryPhone") or {}).get("phoneNumber") or ""
            return {
                "shipName": ship_to.get("fullName") or "",
                "shipPhone": str(phone).strip(),
                "shipEmail": ship_to.get("email") or "",
                "shipAddress1": addr.get("addressLine1") or "",
                "shipAddress2": addr.get("addressLine2") or "",
                "shipCity": addr.get("city") or "",
                "shipState": addr.get("stateOrProvince") or "",
                "shipCountry": addr.get("countryCode") or "",
                "shipZipCode": addr.get("postalCode") or "",
            }
    # Fallback: buyer registration address (not ideal for ship-to)
    buyer = order.get("buyer") or {}
    reg = buyer.get("buyerRegistrationAddress") or {}
    addr = reg.get("contactAddress") or {}
    phone = (reg.get("primaryPhone") or {}).get("phoneNumber") or ""
    return {
        "shipName": reg.get("fullName") or "",
        "shipPhone": str(phone).strip(),
        "shipEmail": reg.get("email") or "",
        "shipAddress1": addr.get("addressLine1") or "",
        "shipAddress2": addr.get("addressLine2") or "",
        "shipCity": addr.get("city") or "",
        "shipState": addr.get("stateOrProvince") or "",
        "shipCountry": addr.get("countryCode") or "",
        "shipZipCode": addr.get("postalCode") or "",
    }


def is_dropship_candidate(order: dict) -> bool:
    """PAID + not fully shipped (NOT_STARTED or IN_PROGRESS)."""
    payment = str(order.get("orderPaymentStatus") or "").upper()
    if payment != "PAID":
        return False
    fulfill = str(order.get("orderFulfillmentStatus") or "").upper()
    return fulfill in ("NOT_STARTED", "IN_PROGRESS")


def build_dropship_payload_from_ebay_order(
    order: dict,
    *,
    sales_channel: str = "eBay",
    delivery_service: str = "DSR",
    order_no: Optional[str] = None,
) -> dict:
    """Map one eBay Fulfillment order → GIGA dropShip-sync body.

    No warehouseCode — GIGA selects the ship-from warehouse.

    eBay defect-rate / order recognition fields (must match eBay template):
      - orderLines[].ebayItemCode  = Item Number = Fulfillment ``legacyItemId``
      - ebayTransactionID          = Transaction ID = Fulfillment ``lineItemId``
    Do **not** derive Transaction ID from Order Number digits.
    """
    ebay_order_id = str(order.get("orderId") or order.get("legacyOrderId") or "").strip()
    giga_no = order_no or make_giga_order_no(ebay_order_id)

    created = order.get("creationDate") or ""
    # GIGA examples use "YYYY-MM-DD HH:MM:SS"
    order_date = created.replace("T", " ").replace("Z", "")
    if "." in order_date:
        order_date = order_date.split(".")[0]

    ship = _ship_to_from_order(order)
    lines = []
    order_total = 0.0
    # Collect eBay Transaction IDs (lineItemId) — required for eBay channel recognition
    transaction_ids: list[str] = []

    for li in order.get("lineItems") or []:
        sku = str(li.get("sku") or "").strip()
        if not sku:
            continue
        qty = int(li.get("quantity") or 0)
        if qty < 1:
            continue
        price = _money((li.get("lineItemCost") or {}).get("value"))
        # Prefer unit-ish total already on lineItemCost; if missing try total
        if price <= 0:
            price = _money((li.get("total") or {}).get("value"))
        order_total += price
        # eBay Item Number (template *eBayItemNumber)
        item_number = str(li.get("legacyItemId") or "").strip()
        # eBay Transaction ID (template *eBayTransactionID) == Fulfillment lineItemId
        txn_id = str(li.get("lineItemId") or "").strip()
        if txn_id:
            transaction_ids.append(txn_id)

        line = {
            "itemPrice": round(price / qty, 2) if qty else round(price, 2),
            "qty": qty,
            "sku": sku,
            "productName": (li.get("title") or sku)[:200],
            "currencyCode": (li.get("lineItemCost") or {}).get("currency") or "USD",
        }
        # GIGA requires pure digits for ebayItemCode when salesChannel=eBay
        if item_number.isdigit():
            line["ebayItemCode"] = item_number
        lines.append(line)

    # Prefer pricingSummary total when present
    ps_total = _money(((order.get("pricingSummary") or {}).get("total") or {}).get("value"))
    if ps_total > 0:
        order_total = ps_total

    # Order-level ebayTransactionID: first line's Transaction ID (lineItemId).
    # Multi-line orders each have their own Transaction ID in eBay reports; API
    # only has one order-level field — first line is used (common for 1-SKU orders).
    ebay_transaction_id = transaction_ids[0] if transaction_ids else ""

    payload = {
        "orderDate": order_date or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "orderNo": giga_no,
        "shipName": ship["shipName"],
        "shipPhone": ship["shipPhone"] or "0000000000",
        "shipEmail": ship["shipEmail"],
        "shipAddress1": ship["shipAddress1"],
        "shipAddress2": ship["shipAddress2"],
        "shipCity": ship["shipCity"],
        "shipCountry": ship["shipCountry"] or "US",
        "shipState": ship["shipState"],
        "shipZipCode": ship["shipZipCode"],
        "salesChannel": sales_channel,
        "orderLines": lines,
        "orderTotal": round(order_total, 2),
        "valueAddedServices": {
            "returnLabelService": False,
            "deliveryService": delivery_service,
        },
        # Explicitly no warehouseCode — GIGA dropship allocates inventory/warehouse.
        # Keep eBay order number for our mapping (not a GIGA template field).
        "_ebayOrderNumber": ebay_order_id,
        "_ebayTransactionIDs": transaction_ids,
    }
    if ebay_transaction_id:
        # Must be the real eBay Transaction ID (lineItemId), not Order Number digits.
        payload["ebayTransactionID"] = ebay_transaction_id

    return payload


def summarize_order_for_list(order: dict) -> dict:
    ship = _ship_to_from_order(order)
    skus = [str(li.get("sku") or "") for li in (order.get("lineItems") or []) if li.get("sku")]
    return {
        "ebay_order_id": order.get("orderId") or order.get("legacyOrderId"),
        "payment": order.get("orderPaymentStatus"),
        "fulfillment": order.get("orderFulfillmentStatus"),
        "created": order.get("creationDate"),
        "skus": skus,
        "ship_to": f"{ship.get('shipCity')}, {ship.get('shipState')} {ship.get('shipCountry')}",
        "total": ((order.get("pricingSummary") or {}).get("total") or {}).get("value"),
    }


def find_inventory_shortages(payload: dict, inventory_by_sku: dict[str, dict]) -> list[str]:
    """Return fail-closed shortage reasons for a prepared dropship payload."""
    from src.clients.dajian_client import extract_available_inventory_quantity

    shortages: list[str] = []
    for line in payload.get("orderLines") or []:
        sku = str(line.get("sku") or "").strip()
        if not sku:
            continue
        inventory = inventory_by_sku.get(sku)
        if not isinstance(inventory, dict):
            shortages.append(f"{sku}: inventory response missing")
            continue
        requested = max(1, int(line.get("qty") or 1))
        available = extract_available_inventory_quantity(inventory)
        if available < requested:
            shortages.append(f"{sku}: requested {requested}, available {available}")
    return shortages


# ─── Persistence ───────────────────────────────────────────────────────────

def ensure_fulfillment_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS giga_fulfillment_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ebay_order_id TEXT NOT NULL,
            giga_order_no TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT,
            last_error TEXT,
            tracking_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ebay_order_id),
            UNIQUE(giga_order_no)
        )
        """
    )
    conn.commit()


def record_fulfillment_attempt(
    conn: sqlite3.Connection,
    *,
    ebay_order_id: str,
    giga_order_no: str,
    status: str,
    payload: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    ensure_fulfillment_table(conn)
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    conn.execute(
        """
        INSERT INTO giga_fulfillment_orders
            (ebay_order_id, giga_order_no, status, payload_json, last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ebay_order_id) DO UPDATE SET
            giga_order_no=excluded.giga_order_no,
            status=excluded.status,
            payload_json=COALESCE(excluded.payload_json, giga_fulfillment_orders.payload_json),
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            ebay_order_id,
            giga_order_no,
            status,
            json.dumps(payload or {}, ensure_ascii=False) if payload is not None else None,
            error,
            now,
        ),
    )
    conn.commit()


def reserve_fulfillment_push(
    conn: sqlite3.Connection,
    *,
    ebay_order_id: str,
    giga_order_no: str,
    payload: dict,
) -> bool:
    """Atomically reserve one eBay order before the non-idempotent GIGA write."""
    ensure_fulfillment_table(conn)
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    payload_json = json.dumps(payload, ensure_ascii=False)
    cursor = conn.execute(
        """
        UPDATE giga_fulfillment_orders
        SET status = 'push_in_progress', payload_json = ?, last_error = NULL,
            updated_at = ?
        WHERE ebay_order_id = ?
          AND giga_order_no = ?
          AND status IN (
              'dry_run_ready', 'validation_failed', 'stock_blocked',
              'stock_check_failed', 'push_failed'
          )
        """,
        (payload_json, now, ebay_order_id, giga_order_no),
    )
    if cursor.rowcount == 1:
        conn.commit()
        return True

    try:
        conn.execute(
            """
            INSERT INTO giga_fulfillment_orders
                (ebay_order_id, giga_order_no, status, payload_json, updated_at)
            VALUES (?, ?, 'push_in_progress', ?, ?)
            """,
            (ebay_order_id, giga_order_no, payload_json, now),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    conn.commit()
    return True


def get_fulfillment_row(conn: sqlite3.Connection, ebay_order_id: str) -> Optional[dict]:
    ensure_fulfillment_table(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM giga_fulfillment_orders WHERE ebay_order_id = ?",
        (ebay_order_id,),
    ).fetchone()
    return dict(row) if row else None


# ─── eBay order fetch ──────────────────────────────────────────────────────

def fetch_ebay_orders(
    *,
    days: int = 14,
    limit_pages: int = 20,
    oauth=None,
) -> list[dict]:
    """Pull recent orders from eBay Sell Fulfillment API."""
    import os

    import requests

    from src.services.ebay_auth import EbayOAuthService

    if oauth is None:
        oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    token = oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    base = "https://api.ebay.com"
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    date_from = (now_utc - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    all_orders: list[dict] = []
    offset = 0
    page_limit = 50
    pages = 0
    while pages < limit_pages:
        params = {
            "filter": f"creationdate:[{date_from}..]",
            "limit": str(page_limit),
            "offset": str(offset),
        }
        r = requests.get(
            f"{base}/sell/fulfillment/v1/order",
            headers=headers,
            params=params,
            timeout=60,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Fulfillment API {r.status_code}: {r.text[:400]}")
        data = r.json()
        orders = data.get("orders") or []
        total = int(data.get("total") or 0)
        all_orders.extend(orders)
        pages += 1
        if len(all_orders) >= total or len(orders) < page_limit:
            break
        offset += page_limit
    return all_orders


def plan_dropship_batch(
    orders: list[dict],
    *,
    only_order_id: Optional[str] = None,
    skip_already_pushed: bool = True,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict]:
    """Filter candidates and build dry-run plans.

    Each plan: {
      ebay_order_id, summary, payload, validation_errors, prior_status
    }
    """
    from src.clients.dajian_client import DaJianClient

    plans = []
    for order in orders:
        if not is_dropship_candidate(order):
            continue
        ebay_id = str(order.get("orderId") or order.get("legacyOrderId") or "")
        if only_order_id and ebay_id != only_order_id and order.get("legacyOrderId") != only_order_id:
            continue

        prior = None
        if conn is not None:
            prior = get_fulfillment_row(conn, ebay_id)
            # still_processing: already on GIGA, waiting for tracking — do not re-push
            # push_unknown: write may have been accepted (timeout) — do not re-push
            if skip_already_pushed and prior and prior.get("status") not in RETRYABLE_FULFILLMENT_STATUSES:
                continue

        payload = build_dropship_payload_from_ebay_order(order)
        errors = DaJianClient.validate_dropship_payload(payload)
        plans.append(
            {
                "ebay_order_id": ebay_id,
                "summary": summarize_order_for_list(order),
                "payload": payload,
                "validation_errors": errors,
                "prior_status": (prior or {}).get("status"),
                "ok": len(errors) == 0,
            }
        )
    return plans
