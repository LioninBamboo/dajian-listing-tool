"""Decide when a sold-through Trading/Motors listing should be relisted.

Motors parts are published with Trading AddFixedPriceItem (SiteID 100). With
OutOfStockControl unset, selling the last unit Completes the listing
(EndingReason Sold) instead of leaving it Active at quantity 0. Inventory
Offer lookups then return NOT_FOUND. That is not a seller delist.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Optional

SOLD_ENDING_REASONS = {"sold", "selltohighbidder"}
SELLER_END_REASONS = {"notavailable", "incorrect", "lostorbroken", "otherlistingerror"}
DEFAULT_SOLD_RELIST_LOOKBACK_DAYS = 60


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def _as_int(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def sold_relist_lookback_days() -> int:
    raw = str(os.getenv("EBAY_SOLD_RELIST_LOOKBACK_DAYS", "") or "").strip()
    if not raw:
        return DEFAULT_SOLD_RELIST_LOOKBACK_DAYS
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_SOLD_RELIST_LOOKBACK_DAYS
    if days < 1:
        return DEFAULT_SOLD_RELIST_LOOKBACK_DAYS
    return min(days, 60)


def uses_trading_channel() -> bool:
    """True for stores that publish through Trading (eBay Motors parts)."""
    try:
        from src.utils.store_profile import get_store_profile

        profile = get_store_profile()
    except Exception:
        return False
    return str(getattr(profile, "listing_channel", "") or "").strip().lower() == "trading"


def trading_site_id() -> str:
    """Trading SiteID for this store. Motors parts use 100; inventory stores stay on 0."""
    if not uses_trading_channel():
        return "0"
    try:
        from src.utils.store_profile import get_store_profile

        profile = get_store_profile()
    except Exception:
        return "0"
    site = str(getattr(profile, "ebay_site_id", "") or "").strip()
    return site or "0"


def completed_because_sold(
    listing_status: Any,
    ending_reason: Any = None,
    quantity_sold: Any = None,
) -> bool:
    """True when Trading ended the listing because the quantity sold through.

    Seller ends (NotAvailable / Incorrect / LostOrBroken / OtherListingError)
    are not restock candidates. A Completed fixed-price item often omits
    EndingReason and only reports QuantitySold.
    """
    status = _norm(listing_status)
    reason = _norm(ending_reason)
    sold_qty = _as_int(quantity_sold) or 0
    if status != "completed":
        return False
    if reason in SELLER_END_REASONS:
        return False
    if reason in SOLD_ENDING_REASONS:
        return True
    return sold_qty > 0


def should_mark_delisted(
    *,
    inventory_offer_status: Any,
    trading_status: Any,
) -> bool:
    """Mark DELISTED only with positive end evidence.

    Inventory Offer NOT_FOUND is normal for Trading/Motors listings.
    A Trading GetItem miss (NOT_FOUND, empty, or lookup failure) is not
    enough either — the call can fail on the wrong site or a transient error.
    """
    inventory = _norm(inventory_offer_status)
    trading = _norm(trading_status)
    if trading in {"", "notfound", "unknown", "error"}:
        return inventory == "ended"
    if trading == "ended":
        return True
    return False


def choose_sold_relist_target(
    *,
    ended_listing_id: Any,
    current_listing_id: Any = None,
    current_status: Any = None,
    current_ending_reason: Any = None,
    current_quantity_sold: Any = None,
    current_available: Any = None,
    current_confirmed: bool = True,
) -> tuple[str, str]:
    """Pick the only ItemID that may be relisted, or refuse.

    SoldList keeps the original ended item for the lookback window, and eBay
    accepts RelistFixedPriceItem on that item more than once. If
    ``current_listing_id`` already points at a different listing, relisting
    the SoldList ItemID creates a second live listing.

    Returns ``(action, item_id)``:
    - ``relist``: ``item_id`` is the current ended listing and may be relisted
    - ``revise``: ``item_id`` is already the live listing; change quantity only
    - ``skip``: ``item_id`` is already live and has stock
    - ``abort``: the current listing differs and could not be confirmed
    """
    ended = str(ended_listing_id or "").strip()
    current = str(current_listing_id or "").strip()
    if not current or current == ended:
        return "relist", ended
    if not current_confirmed:
        return "abort", current

    status = _norm(current_status)
    if status in {"", "notfound", "unknown", "error"}:
        return "abort", current
    if status in {"active", "outofstock"}:
        available = _as_int(current_available)
        if available is None:
            return "abort", current
        if available > 0:
            return "skip", current
        return "revise", current
    if completed_because_sold(current_status, current_ending_reason, current_quantity_sold):
        return "relist", current
    return "abort", current


def classify_zero_qty_listing(
    *,
    inventory_offer_status: Any,
    trading_status: Any,
    ending_reason: Any = None,
    quantity_sold: Any = None,
) -> str:
    """Return relist, revise, delist, or skip for a zero-available listing."""
    if completed_because_sold(trading_status, ending_reason, quantity_sold):
        return "relist"
    if _norm(trading_status) in {"active", "outofstock"}:
        return "revise"
    if should_mark_delisted(
        inventory_offer_status=inventory_offer_status,
        trading_status=trading_status,
    ):
        return "delist"
    return "skip"


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _child_text(node: ET.Element, *path: str) -> str:
    current = node
    for part in path:
        found = None
        for child in list(current):
            if _local_name(child.tag) == part:
                found = child
                break
        if found is None:
            return ""
        current = found
    return (current.text or "").strip()


def parse_trading_item_xml(xml_text: str) -> Optional[dict]:
    """Parse a GetItem response into listing status fields.

    Returns a NOT_FOUND snapshot when eBay acks Failure or the item is absent.
    Returns None only when the body is not XML.
    """
    if not xml_text or not str(xml_text).strip():
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    ack = ""
    item = None
    for el in root.iter():
        name = _local_name(el.tag)
        if name == "Ack" and el.text and not ack:
            ack = el.text.strip()
        if name == "Item" and item is None:
            item = el
    if ack.lower() == "failure" or item is None:
        return {"listing_status": "NOT_FOUND"}

    quantity = _as_int(_child_text(item, "Quantity"))
    sold = _as_int(_child_text(item, "SellingStatus", "QuantitySold"))
    available = _as_int(_child_text(item, "QuantityAvailable"))
    if available is None and quantity is not None:
        available = max(0, quantity - (sold or 0))

    return {
        "item_id": _child_text(item, "ItemID"),
        "sku": _child_text(item, "SKU"),
        "listing_status": _child_text(item, "SellingStatus", "ListingStatus") or "NOT_FOUND",
        "ending_reason": _child_text(item, "ListingDetails", "EndingReason"),
        "quantity": quantity,
        "quantity_sold": sold if sold is not None else 0,
        "available": available,
    }


def trading_ack_ok(xml_text: str) -> bool:
    body = str(xml_text or "")
    return "<Ack>Success</Ack>" in body or "<Ack>Warning</Ack>" in body


def extract_item_id(xml_text: str) -> str:
    match = re.search(r"<ItemID>\s*(\d+)\s*</ItemID>", str(xml_text or ""))
    return match.group(1) if match else ""
