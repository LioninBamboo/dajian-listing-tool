"""Build eBay Trading API AddFixedPriceItem XML for eBay Motors parts (P0-A).

Motors parts categories can only be published via the legacy Trading API on
SiteID 100 — the Sell Inventory API rejects them (errorId 25005). This module
holds the PURE XML builders (unit-tested); the HTTP call lives in
real_ebay_client.add_fixed_price_item_motors.

Verified live 2026-07-23: a trailer hitch published to 33653 with 77 fitment
entries once the return policy was seller-paid (P&A requirement). See
docs/HANDOFF_MULTI_STORE_20260723.md §6.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence
from xml.sax.saxutils import escape

# eBay ConditionID for "New". Motors parts are almost always new here.
CONDITION_NEW = "1000"


def format_compatibility_list(
    entries: Optional[Sequence[Mapping[str, Any]]],
    *,
    replace_all: bool = False,
) -> str:
    """Render collected fitment into <ItemCompatibilityList>.

    ``entries`` is ``motorsCompatibility.compatibleProducts``: each item has
    ``compatibilityProperties: [{name, value}]`` (Year/Make/Model/Trim/Engine).
    Returns "" when there is nothing to render (universal-fit part).
    """
    blocks: List[str] = []
    for e in entries or []:
        props = (e or {}).get("compatibilityProperties") or []
        prop_map = {
            str(p.get("name") or "").strip().lower(): str(p.get("value") or "").strip()
            for p in props
        }
        if props and any(not prop_map.get(required) for required in ("year", "make", "model")):
            raise ValueError("Motors compatibility rows require Year, Make, and Model")
        nv = "".join(
            f"<NameValueList><Name>{escape(str(p['name']))}</Name>"
            f"<Value>{escape(str(p['value']))}</Value></NameValueList>"
            for p in props
            if p.get("name") and p.get("value")
        )
        notes = (e or {}).get("notes") or (e or {}).get("compatibilityNotes")
        if notes:
            nv += f"<CompatibilityNotes>{escape(str(notes))}</CompatibilityNotes>"
        if nv:
            blocks.append(f"<Compatibility>{nv}</Compatibility>")
    if not blocks:
        return ""
    replace_all_xml = "<ReplaceAll>true</ReplaceAll>" if replace_all else ""
    return f"<ItemCompatibilityList>{replace_all_xml}{''.join(blocks)}</ItemCompatibilityList>"


def _aspects_xml(aspects: Optional[Mapping[str, Any]]) -> str:
    rows: List[str] = []
    for name, value in (aspects or {}).items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for v in values:
            v = str(v).strip()
            if v:
                rows.append(
                    f"<NameValueList><Name>{escape(str(name))}</Name>"
                    f"<Value>{escape(v)}</Value></NameValueList>"
                )
    return f"<ItemSpecifics>{''.join(rows)}</ItemSpecifics>" if rows else ""


def _pictures_xml(image_urls: Optional[Sequence[str]]) -> str:
    urls = [u for u in (image_urls or []) if u][:24]
    if not urls:
        return ""
    inner = "".join(f"<PictureURL>{escape(u)}</PictureURL>" for u in urls)
    return f"<PictureDetails>{inner}</PictureDetails>"


def parse_trading_item_price(payload: str) -> Optional[float]:
    """Read CurrentPrice, falling back to StartPrice, from a GetItem body.

    Only Success/Warning GetItem payloads are trusted; Failure (or missing Ack)
    bodies can still contain a price-shaped tag that must not be treated as live.
    """
    text = str(payload or "")
    ack_match = re.search(r"<Ack>(\w+)</Ack>", text)
    ack = ack_match.group(1) if ack_match else ""
    if ack not in {"Success", "Warning"}:
        return None
    match = re.search(r"<CurrentPrice[^>]*>([0-9.]+)</CurrentPrice>", text) or re.search(
        r"<StartPrice[^>]*>([0-9.]+)</StartPrice>", text
    )
    return float(match.group(1)) if match else None


def build_revise_fixed_price_item_xml(
    *,
    item_id: str,
    description: Optional[str] = None,
    title: Optional[str] = None,
    start_price: Optional[float] = None,
    compatibility: Optional[Sequence[Mapping[str, Any]]] = None,
    replace_all_compatibility: bool = False,
) -> str:
    """Build a minimal ReviseFixedPriceItem request for a live Motors item.

    Only the fields passed are sent — ReviseFixedPriceItem is a partial update, so
    omitting ItemSpecifics/Compatibility/StartPrice leaves the live values untouched.
    When a caller supplies a live compatibility snapshot, ``ReplaceAll=true``
    makes that snapshot authoritative instead of relying on an implicit API
    merge during a listing revision.
    Used to push a corrected description (or a strengthened title) onto an item
    that was published before a template/QC fix, without disturbing its fitment.
    """
    if not item_id:
        raise ValueError("item_id is required")
    parts = [f"<ItemID>{escape(str(item_id))}</ItemID>"]
    if title is not None:
        parts.append(f"<Title>{escape(str(title)[:80])}</Title>")
    if start_price is not None:
        parts.append(f"<StartPrice>{float(start_price):.2f}</StartPrice>")
    if description is not None:
        parts.append(f"<Description><![CDATA[{description}]]></Description>")
    compatibility_xml = format_compatibility_list(
        compatibility,
        replace_all=replace_all_compatibility,
    )
    if compatibility_xml:
        parts.append(compatibility_xml)
    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<ReviseFixedPriceItemRequest xmlns=\"urn:ebay:apis:eBLBaseComponents\">"
        f"<Item>{''.join(parts)}</Item>"
        "</ReviseFixedPriceItemRequest>"
    )


def build_add_fixed_price_item_xml(
    *,
    title: str,
    description: str,
    category_id: str,
    price: float,
    quantity: int,
    policies: Mapping[str, str],
    location: str,
    postal_code: str,
    aspects: Optional[Mapping[str, Any]] = None,
    image_urls: Optional[Sequence[str]] = None,
    compatibility: Optional[Sequence[Mapping[str, Any]]] = None,
    condition_id: str = CONDITION_NEW,
    country: str = "US",
    currency: str = "USD",
) -> str:
    """Build a full AddFixedPriceItem request for an eBay Motors parts listing.

    ``policies`` uses the Sell-API keys (fulfillmentPolicyId / returnPolicyId /
    paymentPolicyId); they map onto Trading's SellerProfiles. For Motors P&A the
    return policy MUST be seller-paid or publish fails compliance.
    """
    if not category_id:
        raise ValueError("category_id is required")
    if not title:
        raise ValueError("title is required")

    ship = escape(str(policies.get("fulfillmentPolicyId", "")))
    ret = escape(str(policies.get("returnPolicyId", "")))
    pay = escape(str(policies.get("paymentPolicyId", "")))
    seller_profiles = (
        "<SellerProfiles>"
        f"<SellerShippingProfile><ShippingProfileID>{ship}</ShippingProfileID></SellerShippingProfile>"
        f"<SellerReturnProfile><ReturnProfileID>{ret}</ReturnProfileID></SellerReturnProfile>"
        f"<SellerPaymentProfile><PaymentProfileID>{pay}</PaymentProfileID></SellerPaymentProfile>"
        "</SellerProfiles>"
    )

    return (
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
        "<AddFixedPriceItemRequest xmlns=\"urn:ebay:apis:eBLBaseComponents\">"
        "<Item>"
        f"<Title>{escape(str(title)[:80])}</Title>"
        f"<Description><![CDATA[{description or ''}]]></Description>"
        f"<PrimaryCategory><CategoryID>{escape(str(category_id))}</CategoryID></PrimaryCategory>"
        f"<StartPrice currencyID=\"{currency}\">{float(price):.2f}</StartPrice>"
        f"<Quantity>{int(quantity)}</Quantity>"
        "<ListingDuration>GTC</ListingDuration>"
        "<ListingType>FixedPriceItem</ListingType>"
        f"<Currency>{currency}</Currency>"
        f"<Country>{country}</Country>"
        f"<Location>{escape(str(location))}</Location>"
        f"<PostalCode>{escape(str(postal_code))}</PostalCode>"
        f"<ConditionID>{condition_id}</ConditionID>"
        f"{_pictures_xml(image_urls)}"
        f"{seller_profiles}"
        f"{_aspects_xml(aspects)}"
        f"{format_compatibility_list(compatibility)}"
        "</Item>"
        "</AddFixedPriceItemRequest>"
    )
