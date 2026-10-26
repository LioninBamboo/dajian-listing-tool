"""Live Inventory write helpers shared by audit_fix and sku_field_edit.

Moved from ``scripts/audit_fix_active_listings.py`` without behavior changes.
"""

from __future__ import annotations

import logging
import os
import re
from copy import deepcopy

from src.services.ebay_category_matcher import create_category_matcher
from src.utils.html_truncator import smart_truncate_html
from src.utils.publish_autofix import (
    SINGLE_VALUE_ASPECTS,
    prepare_ebay_aspects,
    sanitize_single_value_aspects,
    source_combined_aspect_keys,
)
from src.utils.title_sanitizer import normalize_listing_title_for_ebay


_CATEGORY_MATCHER = None


def get_category_matcher():
    global _CATEGORY_MATCHER
    if _CATEGORY_MATCHER is None:
        _CATEGORY_MATCHER = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    return _CATEGORY_MATCHER


def remove_empty_table_rows(html: str) -> str:
    def repl(match):
        row_content = match.group(1)
        tds = re.findall(r'<td\b[^>]*>(.*?)</td>', row_content, flags=re.IGNORECASE | re.DOTALL)
        if len(tds) >= 2:
            val_text = re.sub(r'<[^>]+>', '', tds[1])
            val_text = val_text.replace('&nbsp;', '').replace('&#160;', '').replace('&mdash;', '').replace('&#8212;', '')
            val_text = re.sub(r'[\s\xa0\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]+', '', val_text)
            if not val_text or val_text.strip() in ("", "-", "Does Not Apply"):
                return ""
        
        text = re.sub(r'<[^>]+>', '', row_content)
        text = text.replace('&nbsp;', '').replace('&#160;', '').replace('&mdash;', '').replace('&#8212;', '')
        text = re.sub(r'[\s\xa0\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]+', '', text)
        if not text:
            return ""
        return match.group(0)
    return re.sub(r'<tr\b[^>]*>(.*?)</tr>', repl, html, flags=re.IGNORECASE | re.DOTALL)


def compress_html(html: str) -> str:
    if not html:
        return ""
    html = remove_empty_table_rows(html)
    html = re.sub(r'[\r\n\t]+', ' ', html)
    html = re.sub(r'\s+', ' ', html)
    html = re.sub(r'>\s+<', '><', html)
    return html.strip()


def _select_best_offer(offers, expected_listing_id=None):
    """Prefer the live published offer over stale unpublished leftovers."""
    if not offers:
        return None

    def _rank(offer):
        listing = offer.get("listing") or {}
        listing_id = listing.get("listingId")
        listing_status = listing.get("listingStatus", "")
        status = offer.get("status", "")
        listing_match_rank = 0 if expected_listing_id and listing_id == expected_listing_id else 1
        active_rank = 0 if listing_status == "ACTIVE" else 1
        published_rank = 0 if status == "PUBLISHED" else 1
        return (listing_match_rank, active_rank, published_rank, offer.get("offerId", ""))

    return sorted(offers, key=_rank)[0]


def _fetch_live_listing_context(ebay_client, sku, expected_listing_id=None):
    inventory_item = ebay_client.get_inventory_item(sku) or {}
    offers = ebay_client.get_offers_by_sku(sku) or []
    offer = _select_best_offer(offers, expected_listing_id=expected_listing_id)
    return inventory_item, offer


def _publish_offer_checked(ebay_client, offer_id: str):
    if not hasattr(ebay_client, "publish_offer"):
        raise RuntimeError(f"offer {offer_id}: client has no publish_offer")
    result = ebay_client.publish_offer(offer_id)
    if not result or not result.get("listingId"):
        raise RuntimeError(f"publish_offer({offer_id}) returned {result!r}")
    return result


def resolve_inventory_availability_for_product_put(
    live_inventory,
    *,
    offer_quantity=None,
    allow_unresolved: bool = False,
    fallback_quantity=None,
) -> dict | None:
    """Build a valid availability block for product-only inventory PUT.

    eBay Inventory PUT is replacement-style. Omitting availability triggers 25604
    "Availability not found". Stock safety rules (conservative):

    1. Prefer live inventory quantity when present (including explicit 0).
    2. Else use live offer availableQuantity when present (including 0).
    3. Else do NOT invent stock: return None (caller should skip the PUT),
       unless allow_unresolved=True and fallback_quantity is explicitly provided.

    Default never synthesizes quantity=1. That avoided 25604 but could leave eBay
    sellable when GIGA/Dajian is OOS and both quantity sources were unreadable.
    """
    live = live_inventory if isinstance(live_inventory, dict) else {}
    avail = live.get("availability") if isinstance(live.get("availability"), dict) else {}

    qty = None
    allocation = None
    qty_source = None
    ship = avail.get("shipToLocationAvailability") if isinstance(avail, dict) else None
    if isinstance(ship, dict):
        raw_qty = ship.get("quantity")
        if raw_qty is not None and str(raw_qty).strip() != "":
            try:
                qty = int(float(raw_qty))
                qty_source = "live_inventory"
            except (TypeError, ValueError):
                qty = None
        allocation = ship.get("allocationByFormat")

    if qty is None and offer_quantity is not None and str(offer_quantity).strip() != "":
        try:
            qty = int(float(offer_quantity))
            qty_source = "live_offer"
        except (TypeError, ValueError):
            qty = None

    if qty is None:
        if allow_unresolved and fallback_quantity is not None and str(fallback_quantity).strip() != "":
            try:
                qty = int(float(fallback_quantity))
                qty_source = "explicit_fallback"
            except (TypeError, ValueError):
                return None
        else:
            return None

    if qty < 0:
        qty = 0

    ship_block = {"quantity": qty}
    if isinstance(allocation, dict) and allocation:
        ship_block["allocationByFormat"] = allocation
    elif qty > 0:
        # Match the shape eBay returns for healthy fixed-price inventory items.
        ship_block["allocationByFormat"] = {"auction": 0, "fixedPrice": qty}

    result = {}
    if isinstance(avail, dict):
        for key, value in avail.items():
            if key != "shipToLocationAvailability":
                result[key] = value
    result["shipToLocationAvailability"] = ship_block
    result["_quantity_source"] = qty_source  # stripped before PUT
    return result


def _offer_quantity_hint(ebay_client, sku) -> int | None:
    """Best-effort live offer quantity when inventory availability is incomplete."""
    getter = getattr(ebay_client, "get_offers_by_sku", None)
    if not callable(getter):
        return None
    try:
        offers = getter(sku) or []
    except Exception:
        return None
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        raw = offer.get("availableQuantity")
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _inventory_snapshot_put_payload(live_inventory: dict) -> dict:
    """Keep only fields accepted by Inventory API for a snapshot restore."""
    payload = {}
    for key in (
        "condition",
        "conditionDescription",
        "conditionDescriptors",
        "product",
        "availability",
        "packageWeightAndSize",
        "locale",
        "merchantLocationKey",
    ):
        value = live_inventory.get(key)
        if value is not None:
            payload[key] = deepcopy(value)
    return payload


def _put_inventory_snapshot(ebay_client, sku: str, live_inventory: dict):
    """Restore a protected inventory snapshot after a replacement PUT fails."""
    token = ebay_client.oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }
    url = f"{ebay_client.base_url}/sell/inventory/v1/inventory_item/{sku}"
    return ebay_client.session.put(
        url,
        headers=headers,
        json=_inventory_snapshot_put_payload(live_inventory),
        timeout=120,
    )


def _put_inventory_product_only(
    ebay_client, sku, title, description, aspects, lookup_video=None
):
    """Update only inventory_item.product fields without touching quantity or price."""
    live_inventory = ebay_client.get_inventory_item(sku) or {}
    if not live_inventory:
        raise RuntimeError(f"{sku}: live inventory item not found")

    description = description or ""
    compressed = compress_html(description)
    if len(compressed) <= 4000:
        description = compressed
    else:
        description = smart_truncate_html(description, max_length=4000, min_length=3600)

    cleaned_aspects = {}
    for key, value in (aspects or {}).items():
        if isinstance(value, list):
            values = [str(item).strip() for item in value if str(item).strip()]
        else:
            values = [str(value).strip()] if str(value).strip() else []
        if values:
            cleaned_aspects[str(key)] = values

    forced_single = set(SINGLE_VALUE_ASPECTS) | {"Care Instructions", "Fill Material"}
    preserved_source_aspects = source_combined_aspect_keys(cleaned_aspects)
    sanitize_single_value_aspects(
        cleaned_aspects,
        log=lambda message: logging.info(f"[SANITIZE] {sku}: {message}"),
        multi_value_aspects={key for key in cleaned_aspects if key not in forced_single}
        | preserved_source_aspects,
    )
    prepare_ebay_aspects(
        cleaned_aspects,
        required_aspect_names=(),
        log=lambda message: logging.info(f"[ASPECTS] {sku}: {message}"),
    )

    product = dict(live_inventory.get("product") or {})
    safe_title, _ = normalize_listing_title_for_ebay(title, source_title=title)
    product["title"] = safe_title
    product["description"] = description
    product["aspects"] = cleaned_aspects
    live_video_ids = [
        str(item).strip()
        for item in (product.get("videoIds") or [])
        if str(item).strip()
    ]
    if live_video_ids:
        product["videoIds"] = live_video_ids[:1]
    else:
        from src.clients.real_ebay_client import lookup_stored_video_id as _default_lookup

        lookup = lookup_video or _default_lookup
        local_video_id = lookup(sku)
        if local_video_id:
            product["videoIds"] = [local_video_id]

    payload = {
        "condition": live_inventory.get("condition") or "NEW",
        "product": product,
    }
    # Inventory items with a merchant location require the key on every
    # replacement PUT; omitting it is a common 25604 "Availability not found"
    # trigger even when shipToLocationAvailability.quantity is present.
    for meta_key in ("merchantLocationKey", "locale"):
        meta_val = live_inventory.get(meta_key)
        if meta_val:
            payload[meta_key] = meta_val
    if live_inventory.get("packageWeightAndSize"):
        pkg = dict(live_inventory.get("packageWeightAndSize") or {})
        weight = pkg.get("weight") or {}
        if weight:
            try:
                value = float(weight.get("value", 0))
                if value <= 0 or value > 2000:
                    pkg.pop("weight", None)
                else:
                    pkg["weight"] = {"value": round(value, 2), "unit": weight.get("unit", "POUND")}
            except (TypeError, ValueError):
                pkg.pop("weight", None)

        for dim_key in ("dimensions", "packageDimensions"):
            dims = pkg.get(dim_key) or {}
            if not dims:
                continue
            cleaned_dims = {"unit": dims.get("unit", "INCH")}
            valid = True
            for axis in ("length", "width", "height"):
                try:
                    value = float(dims.get(axis, 0))
                except (TypeError, ValueError):
                    valid = False
                    break
                if value <= 0 or value > 999:
                    valid = False
                    break
                cleaned_dims[axis] = round(value, 2)
            if valid:
                pkg[dim_key] = cleaned_dims
            else:
                pkg.pop(dim_key, None)

        if pkg:
            payload["packageWeightAndSize"] = pkg

    # Inventory PUT is replacement-style: always send a valid availability block
    # so eBay does not return 25604 "Availability not found".
    # Conservative stock rule: only use live inventory qty or live offer qty.
    # If neither is known, skip the PUT rather than inventing quantity=1 (GIGA OOS risk).
    offer_qty = _offer_quantity_hint(ebay_client, sku)
    availability = resolve_inventory_availability_for_product_put(
        live_inventory,
        offer_quantity=offer_qty,
    )
    if not availability:
        raise RuntimeError(
            f"{sku}: skip product-only inventory update — cannot resolve availability "
            f"from live inventory or offer (refusing to invent stock; avoids 25604 false-fix "
            f"and GIGA-OOS / eBay-in-stock mismatch)"
        )
    qty_source = availability.pop("_quantity_source", None)
    payload["availability"] = availability
    if qty_source:
        logging.info(
            f"[AVAIL] {sku}: product-only PUT availability quantity="
            f"{availability.get('shipToLocationAvailability', {}).get('quantity')} "
            f"source={qty_source}"
        )

    token = ebay_client.oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }
    url = f"{ebay_client.base_url}/sell/inventory/v1/inventory_item/{sku}"
    response = ebay_client.session.put(url, headers=headers, json=payload, timeout=120)

    def _is_availability_not_found(resp) -> bool:
        text = str(getattr(resp, "text", "") or "")
        return "25604" in text or "Availability not found" in text

    # One refresh+retry on 25604: live qty / offer qty / merchantLocationKey may
    # have been stale on the first read.
    if response.status_code not in (200, 204) and _is_availability_not_found(response):
        logging.warning(f"[AVAIL] {sku}: 25604 on product-only PUT — refresh and retry once")
        refreshed = ebay_client.get_inventory_item(sku) or {}
        if refreshed:
            live_inventory = refreshed
            for meta_key in ("merchantLocationKey", "locale"):
                meta_val = live_inventory.get(meta_key)
                if meta_val:
                    payload[meta_key] = meta_val
                else:
                    payload.pop(meta_key, None)
            if live_inventory.get("condition"):
                payload["condition"] = live_inventory.get("condition")
        offer_qty = _offer_quantity_hint(ebay_client, sku)
        availability = resolve_inventory_availability_for_product_put(
            live_inventory,
            offer_quantity=offer_qty,
        )
        if not availability:
            raise RuntimeError(
                f"{sku}: skip product-only inventory update after 25604 — cannot resolve "
                f"availability from refreshed live inventory or offer"
            )
        availability.pop("_quantity_source", None)
        payload["availability"] = availability
        response = ebay_client.session.put(url, headers=headers, json=payload, timeout=120)

    if response.status_code not in (200, 204):
        rollback_status = "not_attempted"
        rollback_error = ""
        try:
            rollback_response = _put_inventory_snapshot(ebay_client, sku, live_inventory)
            rollback_status = str(getattr(rollback_response, "status_code", "unknown"))
        except Exception as exc:
            rollback_error = f"; rollback_error={exc}"
        raise RuntimeError(
            f"{sku}: product-only inventory update failed {response.status_code}: "
            f"{response.text[:300]} (rollback_status={rollback_status}{rollback_error})"
        )

    return response, description, cleaned_aspects


def _claim_strip_required_aspect_names(category_id: str | None, matcher_factory=None):
    """Return required aspect names, or None when the taxonomy cannot be trusted.

    None (missing category, throw, or empty lookup) is fail-closed: every
    non-Features aspect is treated as required.
    """
    if not category_id:
        return None
    try:
        matcher = (matcher_factory or get_category_matcher)()
        required_aspects, _ = matcher._get_category_aspects(str(category_id))
        names = [aspect.get("name") for aspect in required_aspects if aspect.get("name")]
        if not names:
            return None
        return names
    except Exception:
        return None


def _update_offer_description_only(ebay_client, offer_id: str, listing_description: str):
    """GET the full offer, change only listingDescription, PUT the body back.

    Does not look up suppliers, recalculate availableQuantity, or rebuild
    pricingSummary. Any exception (including SupplierOutOfStockError if a
    client hook raises it) propagates to the caller for inventory restore.
    """
    from src.utils import claim_strip as cs

    live = ebay_client.get_offer(str(offer_id))
    if not live:
        raise RuntimeError(f"offer {offer_id} GET returned empty")
    payload = cs.offer_description_only_put_payload(live, listing_description)
    token = ebay_client.oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }
    url = f"{ebay_client.base_url}/sell/inventory/v1/offer/{offer_id}"
    response = ebay_client.session.put(url, headers=headers, json=payload, timeout=120)
    if getattr(response, "status_code", 200) not in (200, 204):
        raise RuntimeError(
            f"offer description PUT failed {getattr(response, 'status_code', '?')}: "
            f"{getattr(response, 'text', '')[:200]}"
        )
    return response


def _restore_claim_strip_snapshot(
    *,
    sku: str,
    plan,
    ebay_client,
    offer_id,
    expected_listing_id: str,
):
    """Fresh GET, put back only snapshot description/aspects, republish, read back."""
    from src.utils import claim_strip as cs

    try:
        fresh_inventory, fresh_offer = _fetch_live_listing_context(
            ebay_client,
            sku,
            expected_listing_id=expected_listing_id,
        )
    except Exception as exc:
        return False, {"error": f"restore GET failed: {exc}"}, {}
    if not fresh_inventory or not fresh_offer:
        return False, {"error": "restore GET returned empty inventory/offer"}, {}

    inventory_payload = cs.replace_inventory_product_fields(
        fresh_inventory,
        description=plan.inventory_description_before,
        aspects=plan.aspects_before,
    )
    try:
        put_response = _put_inventory_snapshot(ebay_client, sku, inventory_payload)
        if getattr(put_response, "status_code", 200) not in (200, 204):
            raise RuntimeError(
                f"restore inventory PUT failed {getattr(put_response, 'status_code', '?')}: "
                f"{getattr(put_response, 'text', '')[:200]}"
            )
    except Exception as exc:
        return False, {"error": f"restore inventory PUT failed: {exc}"}, {}

    restore_offer_id = offer_id or (fresh_offer or {}).get("offerId")
    if restore_offer_id:
        try:
            _update_offer_description_only(
                ebay_client,
                restore_offer_id,
                plan.offer_description_before,
            )
        except Exception as exc:
            return False, {"error": f"restore offer PUT failed: {exc}"}, {}
        try:
            _publish_offer_checked(ebay_client, restore_offer_id)
        except Exception as exc:
            return False, {"error": f"restore publish failed: {exc}"}, {}

    try:
        read_inventory, read_offer = _fetch_live_listing_context(
            ebay_client,
            sku,
            expected_listing_id=expected_listing_id,
        )
    except Exception as exc:
        return False, {"error": f"restore readback GET failed: {exc}"}, {}

    restore_plan = cs.SkuPlan(
        sku=plan.sku,
        listing_id=plan.listing_id,
        title=plan.title,
        offer_description_before=plan.offer_description_before,
        offer_description_after=plan.offer_description_before,
        inventory_description_before=plan.inventory_description_before,
        inventory_description_after=plan.inventory_description_before,
        aspects_before=deepcopy(plan.aspects_before),
        aspects_after=deepcopy(plan.aspects_before),
    )
    expected_inventory = cs.replace_inventory_product_fields(
        fresh_inventory,
        description=plan.inventory_description_before,
        aspects=plan.aspects_before,
    )
    expected_offer = deepcopy(fresh_offer)
    expected_offer["listingDescription"] = plan.offer_description_before
    read_product = (read_inventory or {}).get("product") or {}
    snapshot_ok = (
        str((read_offer or {}).get("listingDescription") or "") == plan.offer_description_before
        and str(read_product.get("description") or "") == plan.inventory_description_before
        and {
            str(key): cs.aspect_values(value)
            for key, value in dict(read_product.get("aspects") or {}).items()
        }
        == plan.aspects_before
    )
    checks = cs.verify_readback(
        restore_plan,
        read_inventory,
        read_offer,
        expected_inventory,
        expected_offer,
    )
    if not snapshot_ok or not all(checks.values()):
        return (
            False,
            {
                "error": f"restore readback mismatch: snapshot_ok={snapshot_ok} checks={checks}",
                "readback": checks,
            },
            checks,
        )
    return True, {"readback": checks}, checks
