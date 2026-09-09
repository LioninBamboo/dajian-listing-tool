"""Collect a product draft from an existing eBay listing URL.

Blind-box (instance C) sources drafts by pasting an eBay item link: this
fetches the item's images and item specifics via the Browse API and maps them
onto the CollectedProduct shape used by the rest of the pipeline.

Design notes:
- Uses an **Application token** (client-credentials), so it needs no seller
  OAuth — it works on any instance, including for offline-ish testing.
- ``get_item_by_legacy_id`` takes the numeric id straight from the listing URL.
- COMPLIANCE: images and copy pulled from someone else's live listing are for
  *drafting only*. Third-party listing images are copyright-protected and
  POP MART-style items are VeRO-active; the mapped result flags images as
  needing manual replacement before publish. See BLINDBOX_INSTANCE_PLAN §1.3.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

BROWSE_ITEM_LEGACY_ENDPOINT = (
    "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id"
)
BROWSE_ITEM_GROUP_ENDPOINT = (
    "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group"
)
COLLECTED_SKU_PREFIX = "EB-"  # eBay-sourced; distinguishes from Dajian/GC SKUs

# eBay legacy item ids are 11-12 digit numbers.
_LEGACY_ID_RE = re.compile(r"(?<!\d)(\d{9,15})(?!\d)")


class EbayLinkParseError(ValueError):
    """The pasted value did not contain a recognizable eBay item id."""


def parse_item_id(url_or_id: str) -> str:
    """Extract the numeric legacy item id from a URL, a bare id, or v1|id|0.

    Accepts:
      - https://www.ebay.com/itm/256123456789
      - https://www.ebay.com/itm/Some-Product-Slug/256123456789?hash=abc
      - https://www.ebay.com/itm/256123456789?var=...
      - v1|256123456789|0   (Browse resource id)
      - 256123456789        (bare)
    """
    raw = str(url_or_id or "").strip()
    if not raw:
        raise EbayLinkParseError("empty input")

    # Browse resource id form: v1|<legacy>|<variation>
    if raw.lower().startswith("v1|"):
        parts = raw.split("|")
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]

    # Strip query/fragment so ids in query strings don't outrank the path id.
    path = raw.split("?", 1)[0].split("#", 1)[0]

    # Prefer the last all-digit path segment (…/itm/<slug>/<id>).
    segments = [s for s in path.split("/") if s]
    for seg in reversed(segments):
        if seg.isdigit() and 9 <= len(seg) <= 15:
            return seg

    # Fall back to the first id-shaped run anywhere in the original string.
    m = _LEGACY_ID_RE.search(raw)
    if m:
        return m.group(1)

    raise EbayLinkParseError(f"no eBay item id found in {url_or_id!r}")


def parse_variation_id(url_or_id: str) -> Optional[str]:
    """Extract the variation id from a URL's ``var=`` query param, if present.

    Multi-variation listings (…/itm/<groupId>?var=<variationId>) need the
    variation id passed to the Browse API, else it rejects the group id.
    """
    raw = str(url_or_id or "")
    m = re.search(r"[?&]var=(\d{6,})", raw)
    return m.group(1) if m else None


def sku_for_item_id(item_id: str, variation_id: Optional[str] = None) -> str:
    if variation_id:
        return f"{COLLECTED_SKU_PREFIX}{item_id}-{variation_id}"
    return f"{COLLECTED_SKU_PREFIX}{item_id}"


def _first_price(item: Dict[str, Any]) -> float:
    price = item.get("price") or {}
    try:
        return round(float(price.get("value", 0) or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _shipping_cost(item: Dict[str, Any]) -> float:
    """Cheapest shipping charge from the source listing (0 if free/unknown).

    Competitors in this category hide margin in shipping, so the collected
    shipping cost is essential for undercutting the buyer's TRUE landed cost
    (price + shipping), not the item price alone. See undercut_pricing.
    """
    costs = []
    for opt in item.get("shippingOptions") or []:
        sc = (opt or {}).get("shippingCost") or {}
        try:
            costs.append(float(sc.get("value", 0) or 0))
        except (TypeError, ValueError):
            continue
    return round(min(costs), 2) if costs else 0.0


def _collect_images(item: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    main = (item.get("image") or {}).get("imageUrl")
    if main:
        urls.append(main)
    for extra in item.get("additionalImages") or []:
        u = (extra or {}).get("imageUrl")
        if u and u not in urls:
            urls.append(u)
    return urls


def _collect_aspects(item: Dict[str, Any]) -> Dict[str, str]:
    """localizedAspects[] -> {name: value} item specifics."""
    aspects: Dict[str, str] = {}
    for asp in item.get("localizedAspects") or []:
        name = str((asp or {}).get("name", "")).strip()
        value = str((asp or {}).get("value", "")).strip()
        if name and value and name not in aspects:
            aspects[name] = value
    return aspects


def map_to_collected_fields(item: Dict[str, Any], *, source_url: str = "") -> Dict[str, Any]:
    """Map a Browse API item resource onto CollectedProduct-shaped fields."""
    item_id = str(item.get("legacyItemId") or "").strip()
    # Browse resource id is v1|<legacy>|<var>; recover legacy + variation ids.
    rid = str(item.get("itemId") or "")
    parts = rid.split("|")
    if not item_id:
        item_id = parts[1] if len(parts) >= 2 and parts[1].isdigit() else ""
    variation_id = ""
    if len(parts) >= 3 and parts[2].isdigit() and int(parts[2]) > 0:
        variation_id = parts[2]

    description = item.get("description") or item.get("shortDescription") or ""
    condition = str(item.get("condition") or "").strip()
    brand = ""
    aspects = _collect_aspects(item)
    for key in ("Brand", "brand"):
        if aspects.get(key):
            brand = aspects[key]
            break

    price = _first_price(item)
    shipping = _shipping_cost(item)
    return {
        "sku": sku_for_item_id(item_id, variation_id) if item_id else "",
        "item_id": item_id,
        "variation_id": variation_id,
        "title": str(item.get("title") or "").strip(),
        "price": price,
        "shipping": shipping,
        "total_landed": round(price + shipping, 2),
        "images": _collect_images(item),
        "description": description,
        "attributes": aspects,          # item specifics live here
        "condition": condition,
        "brand": brand,
        "category_id": str(item.get("categoryId") or "").strip(),
        "category_path": str(item.get("categoryPath") or "").strip(),
        "item_location_country": str((item.get("itemLocation") or {}).get("country") or ""),
        "url": source_url or item.get("itemWebUrl") or "",
    }


def fetch_item(
    item_id: str,
    *,
    variation_id: Optional[str] = None,
    token: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch a Browse API item resource by legacy id. Raises on HTTP failure.

    ``variation_id`` targets one variation of a multi-variation listing. Without
    it, a variation group id is rejected (errorId 11006); we then fall back to
    ``get_items_by_item_group`` and take the first variation.
    """
    import os
    import requests
    from src.services.ebay_auth import EbayOAuthService

    env = environment or os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    if token is None:
        # App token: Browse works with client-credentials, no seller OAuth.
        token = EbayOAuthService(env).get_application_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {"legacy_item_id": item_id}
    if variation_id:
        params["legacy_variation_id"] = variation_id
    resp = requests.get(BROWSE_ITEM_LEGACY_ENDPOINT, headers=headers, params=params, timeout=20, verify=False)

    if resp.status_code == 400 and not variation_id:
        errs = resp.json().get("errors", []) if resp.headers.get("content-type", "").startswith("application/json") else []
        if any(e.get("errorId") == 11006 for e in errs):
            # Variation group without a chosen variation: take the first one.
            gr = requests.get(BROWSE_ITEM_GROUP_ENDPOINT, headers=headers,
                              params={"item_group_id": item_id}, timeout=20, verify=False)
            gr.raise_for_status()
            items = gr.json().get("items") or []
            if not items:
                raise EbayLinkParseError(f"item group {item_id} returned no variations")
            return items[0]

    resp.raise_for_status()
    return resp.json()


def collect_from_url(
    url: str, *, token: Optional[str] = None, environment: Optional[str] = None
) -> Dict[str, Any]:
    """Parse the URL, fetch ONE item/variation, return CollectedProduct fields.

    For a multi-variation listing this returns a single variation (the one in
    ``var=``, else the first). Use ``collect_group_from_url`` to get them all.
    """
    item_id = parse_item_id(url)
    variation_id = parse_variation_id(url)
    item = fetch_item(item_id, variation_id=variation_id, token=token, environment=environment)
    return map_to_collected_fields(item, source_url=url if url.startswith("http") else "")


def fetch_item_group(
    item_id: str, *, token: Optional[str] = None, environment: Optional[str] = None
) -> Dict[str, Any]:
    """Fetch every variation of a multi-variation listing. Raises on HTTP failure."""
    import os
    import requests
    from src.services.ebay_auth import EbayOAuthService

    env = environment or os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    if token is None:
        token = EbayOAuthService(env).get_application_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    resp = requests.get(BROWSE_ITEM_GROUP_ENDPOINT, headers=headers,
                        params={"item_group_id": item_id}, timeout=30, verify=False)
    resp.raise_for_status()
    return resp.json()


def collect_group_from_url(
    url: str, *, token: Optional[str] = None, environment: Optional[str] = None
) -> Dict[str, Any]:
    """Collect ALL variations of a listing (blind-box series = one variation each).

    Returns the group's shared fields plus a ``variations`` list, each entry a
    CollectedProduct-shaped dict (own price/shipping/total_landed/aspects/images).
    Single-variation (non-group) URLs come back as a one-entry group.
    """
    item_id = parse_item_id(url)
    src = url if url.startswith("http") else ""
    try:
        group = fetch_item_group(item_id, token=token, environment=environment)
        raw_items = group.get("items") or []
    except Exception:
        raw_items = []

    if not raw_items:  # not a group — fall back to a single-item collection
        one = collect_from_url(url, token=token, environment=environment)
        return {
            "item_group_id": item_id,
            "title": one.get("title", ""),
            "description": one.get("description", ""),
            "images": one.get("images", []),
            "variation_count": 1,
            "variations": [one],
        }

    variations = [map_to_collected_fields(it, source_url=src) for it in raw_items]
    common_desc = ""
    cds = group.get("commonDescriptions") or []
    if cds:
        common_desc = cds[0].get("description", "") or ""
    all_images: List[str] = []
    for v in variations:
        for img in v.get("images", []):
            if img not in all_images:
                all_images.append(img)
    return {
        "item_group_id": item_id,
        "title": str(group.get("title") or (variations[0]["title"] if variations else "")).strip(),
        "description": common_desc or (variations[0]["description"] if variations else ""),
        "images": all_images,
        "variation_count": len(variations),
        "variations": variations,
    }
