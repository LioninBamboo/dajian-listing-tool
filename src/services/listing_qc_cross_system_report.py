"""Read-only reconciliation evidence for eBay/GIGA listing QC.

The report deliberately separates local source snapshots from live API
evidence.  A local ``stock`` value is useful as a fallback, but it is not
treated as a fresh GIGA inventory reading.  Likewise, an absent eBay
inventory item is ``not_published`` only when the local row is not published;
for a published row it remains an evidence gap rather than a zero quantity.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _has_arrival(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    if isinstance(value, Mapping):
        return any(value.get(key) not in (None, "", 0, []) for key in (
            "arrivalDate", "nextArrivalDate", "date", "quantity", "qty", "inventory"
        )) or bool(value)
    return True


def extract_giga_inventory_snapshot(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a DaJian inventory response without making assumptions."""

    data = _mapping(payload)
    buyer = _mapping(data.get("buyerInventoryInfo"))
    seller = _mapping(data.get("sellerInventoryInfo"))

    buyer_quantity = _integer(buyer.get("totalBuyerAvailableInventory"))
    seller_quantity = _integer(seller.get("sellerAvailableInventory"))
    # Match the production inventory client: a zero buyer allocation must
    # fall back to seller-available inventory before calling the SKU OOS.
    quantity = buyer_quantity
    quantity_source = "buyerInventoryInfo.totalBuyerAvailableInventory" if buyer_quantity is not None else ""
    if buyer_quantity is None or buyer_quantity <= 0:
        if seller_quantity is not None:
            quantity = seller_quantity
            quantity_source = "sellerInventoryInfo.sellerAvailableInventory"

    future_quantity = _integer(buyer.get("totalFutureInventory"))
    arrival_value = seller.get("nextArrivalInventory")
    arrival_present = bool(future_quantity and future_quantity > 0) or _has_arrival(arrival_value)
    available_flag = data.get("skuAvailable")
    status = "unknown" if quantity is None else ("in_stock" if quantity > 0 else "out_of_stock")

    return {
        "quantity": quantity,
        "quantity_source": quantity_source or None,
        "buyer_quantity": buyer_quantity,
        "seller_quantity": seller_quantity,
        "status": status,
        "sku_available": available_flag if isinstance(available_flag, bool) else None,
        "future_quantity": future_quantity,
        "arrival_present": arrival_present,
        "arrival_evidence": arrival_value if arrival_present else None,
        "raw_present": bool(data),
    }


def extract_ebay_quantity(inventory_item: Mapping[str, Any] | None) -> int | None:
    """Read the eBay Inventory API quantity from known response locations."""

    data = _mapping(inventory_item)
    availability = _mapping(data.get("availability"))
    ship_to = _mapping(availability.get("shipToLocationAvailability"))
    for value in (
        ship_to.get("quantity"),
        availability.get("quantity"),
        data.get("quantity"),
    ):
        quantity = _integer(value)
        if quantity is not None:
            return quantity
    return None


def extract_ebay_offer_snapshot(offer: Mapping[str, Any] | None) -> dict[str, Any]:
    data = _mapping(offer)
    listing = _mapping(data.get("listing"))
    return {
        "offer_id": data.get("offerId"),
        "offer_status": data.get("status"),
        "listing_status": listing.get("listingStatus") or data.get("listingStatus"),
        "listing_id": listing.get("listingId") or data.get("listingId"),
        "marketplace_id": data.get("marketplaceId"),
    }


def _source_snapshot(product: Mapping[str, Any]) -> dict[str, Any]:
    optimization = _mapping(product.get("optimization"))
    source_fields = {
        "sku": str(product.get("sku") or ""),
        "title": str(product.get("title") or ""),
        "description": str(product.get("description") or ""),
        "attributes": _mapping(product.get("attributes")),
        "specs": _mapping(product.get("specs")),
        "url": str(product.get("url") or ""),
        "images": _list_value(product.get("images")),
        "videos": _list_value(product.get("videos")),
    }
    encoded = json.dumps(source_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    source_url = source_fields["url"]
    parsed = urlparse(source_url) if source_url else None
    host = (parsed.hostname or "").lower() if parsed else ""
    missing = [
        key for key in ("title", "description", "attributes", "specs", "url")
        if not source_fields[key]
    ]
    return {
        "source_snapshot_fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "source_snapshot_status": "complete" if not missing else "incomplete",
        "source_snapshot_missing": missing,
        "source_url": source_url or None,
        "source_host": host or None,
        "seller_evidence": host or None,
        "seller_identity_status": "host_only" if host else "unknown",
        "source_image_count": len(source_fields["images"]),
        "source_video_count": len(source_fields["videos"]),
        "source_facts_present": bool(optimization.get("source_facts")),
    }


def _local_stock(product: Mapping[str, Any]) -> int | None:
    return _integer(product.get("stock"))


def build_reconciliation_row(
    product: Mapping[str, Any],
    *,
    giga_inventory: Mapping[str, Any] | None = None,
    ebay_inventory: Mapping[str, Any] | None = None,
    ebay_offer: Mapping[str, Any] | None = None,
    giga_fetch_error: str | None = None,
    ebay_fetch_error: str | None = None,
) -> dict[str, Any]:
    """Build one conservative, action-free reconciliation row."""

    sku = str(product.get("sku") or "").strip()
    source = _source_snapshot(product)
    local_stock = _local_stock(product)
    giga_live = extract_giga_inventory_snapshot(giga_inventory) if giga_inventory is not None else None
    if giga_live is None:
        giga_quantity = local_stock
        # Local stock remains visible as a fallback datum, but cannot drive a
        # cross-system mismatch when the fresh GIGA response is unavailable.
        giga_status = "unknown"
        giga_evidence = "local_collected_products.stock" if local_stock is not None else "missing"
        arrival_present = False
        giga_quantity_source = None
    else:
        giga_quantity = giga_live["quantity"]
        giga_status = giga_live["status"]
        giga_evidence = "giga_inventory_api"
        arrival_present = bool(giga_live["arrival_present"])
        giga_quantity_source = giga_live["quantity_source"]

    is_published = str(product.get("status") or "").upper() == "PUBLISHED" or bool(product.get("listing_id"))
    ebay_quantity = extract_ebay_quantity(ebay_inventory) if ebay_inventory is not None else None
    if ebay_inventory is None and not is_published:
        ebay_status = "not_published"
        ebay_evidence = "local_status"
    elif ebay_inventory is None:
        ebay_status = "unknown"
        ebay_evidence = "missing_live_inventory"
    else:
        ebay_status = "unknown" if ebay_quantity is None else ("in_stock" if ebay_quantity > 0 else "out_of_stock")
        ebay_evidence = "ebay_inventory_api" if ebay_quantity is not None else "ebay_inventory_without_quantity"

    if not is_published:
        reconciliation = "not_published"
    elif giga_status == "in_stock" and ebay_status == "out_of_stock":
        reconciliation = "giga_in_stock_ebay_zero"
    elif giga_status == "out_of_stock" and ebay_status == "in_stock":
        reconciliation = "giga_oos_ebay_positive"
    elif giga_status == "in_stock" and ebay_status == "in_stock":
        reconciliation = "both_in_stock"
    elif giga_status == "out_of_stock" and ebay_status == "out_of_stock":
        reconciliation = "both_out_of_stock"
    else:
        reconciliation = "unknown_evidence"

    risk_flags: list[str] = []
    if source["source_snapshot_status"] != "complete":
        risk_flags.append("source_snapshot_incomplete")
    if not source["source_url"]:
        risk_flags.append("source_url_missing")
    if source["seller_identity_status"] != "host_only":
        risk_flags.append("seller_identity_unknown")
    if giga_live is None:
        risk_flags.append("giga_live_inventory_missing")
    if giga_fetch_error:
        risk_flags.append("giga_fetch_failed")
    if is_published and ebay_inventory is None:
        risk_flags.append("ebay_live_inventory_missing")
    if ebay_fetch_error:
        risk_flags.append("ebay_fetch_failed")
    if reconciliation == "giga_in_stock_ebay_zero":
        risk_flags.append("do_not_end_listing")
    if reconciliation == "giga_oos_ebay_positive":
        risk_flags.append("inventory_overhang")
    if giga_live is None and local_stock in (99, 999):
        risk_flags.append("local_stock_may_be_default_sentinel")

    if reconciliation == "giga_in_stock_ebay_zero":
        decision = "hold_and_reconcile"
    elif giga_status == "out_of_stock" and arrival_present and is_published:
        decision = "preserve_and_manual_review"
    elif giga_status == "out_of_stock" and is_published:
        decision = "candidate_for_end_after_manual_confirmation"
    elif reconciliation == "unknown_evidence":
        decision = "collect_evidence"
    elif not is_published:
        decision = "not_published"
    else:
        decision = "no_action"

    offer = extract_ebay_offer_snapshot(ebay_offer)
    return {
        "sku": sku,
        "local_status": product.get("status"),
        "listing_id": product.get("listing_id"),
        "source": source,
        "local_stock": local_stock,
        "giga": {
            "quantity": giga_quantity,
            "status": giga_status,
            "local_snapshot_status": (
                "unknown" if local_stock is None else ("in_stock" if local_stock > 0 else "out_of_stock")
            ),
            "evidence": giga_evidence,
            "quantity_source": giga_quantity_source,
            "arrival_present": arrival_present,
        },
        "ebay": {
            "quantity": ebay_quantity,
            "status": ebay_status,
            "evidence": ebay_evidence,
            "offer": offer,
        },
        "reconciliation": reconciliation,
        "decision": decision,
        "risk_flags": list(dict.fromkeys(risk_flags)),
    }


def build_cross_system_report(
    products: list[Mapping[str, Any]],
    *,
    giga_inventory_by_sku: Mapping[str, Mapping[str, Any]] | None = None,
    ebay_inventory_by_sku: Mapping[str, Mapping[str, Any]] | None = None,
    ebay_offer_by_sku: Mapping[str, Mapping[str, Any]] | None = None,
    giga_errors: Mapping[str, str] | None = None,
    ebay_errors: Mapping[str, str] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    giga_inventory_by_sku = giga_inventory_by_sku or {}
    ebay_inventory_by_sku = ebay_inventory_by_sku or {}
    ebay_offer_by_sku = ebay_offer_by_sku or {}
    giga_errors = giga_errors or {}
    ebay_errors = ebay_errors or {}

    rows = [
        build_reconciliation_row(
            product,
            giga_inventory=giga_inventory_by_sku.get(str(product.get("sku") or "")),
            ebay_inventory=ebay_inventory_by_sku.get(str(product.get("sku") or "")),
            ebay_offer=ebay_offer_by_sku.get(str(product.get("sku") or "")),
            giga_fetch_error=giga_errors.get(str(product.get("sku") or "")),
            ebay_fetch_error=ebay_errors.get(str(product.get("sku") or "")),
        )
        for product in products
    ]

    def count(predicate):
        return sum(1 for row in rows if predicate(row))

    summary = {
        "total": len(rows),
        "source_snapshot_complete": count(lambda r: r["source"]["source_snapshot_status"] == "complete"),
        "source_url_present": count(lambda r: bool(r["source"]["source_url"])),
        "seller_host_evidence": count(lambda r: r["source"]["seller_identity_status"] == "host_only"),
        "giga_live_evidence": count(lambda r: r["giga"]["evidence"] == "giga_inventory_api"),
        "giga_in_stock": count(lambda r: r["giga"]["status"] == "in_stock"),
        "giga_out_of_stock": count(lambda r: r["giga"]["status"] == "out_of_stock"),
        "giga_arrival_present": count(lambda r: r["giga"]["arrival_present"]),
        "ebay_quantity_evidence": count(lambda r: r["ebay"]["evidence"] == "ebay_inventory_api"),
        "ebay_zero_quantity": count(lambda r: r["ebay"]["quantity"] == 0),
        "giga_in_stock_ebay_zero": count(lambda r: r["reconciliation"] == "giga_in_stock_ebay_zero"),
        "giga_oos_ebay_positive": count(lambda r: r["reconciliation"] == "giga_oos_ebay_positive"),
        "unknown_evidence": count(lambda r: r["reconciliation"] == "unknown_evidence"),
        "not_published": count(lambda r: r["reconciliation"] == "not_published"),
        "hold_and_reconcile": count(lambda r: r["decision"] == "hold_and_reconcile"),
        "manual_review": count(lambda r: r["decision"] in {"preserve_and_manual_review", "candidate_for_end_after_manual_confirmation"}),
    }
    return {
        "report_version": "listing-qc-cross-system-v1",
        "mode": "read_only_reconciliation",
        "scope": {"skus": [row["sku"] for row in rows], "count": len(rows)},
        "metadata": dict(metadata or {}),
        "summary": summary,
        "rows": rows,
        "policy": {
            "local_stock_is_fallback": True,
            "missing_live_quantity_is_not_zero": True,
            "giga_in_stock_ebay_zero_action": "hold_and_reconcile",
            "giga_oos_with_arrival_action": "preserve_and_manual_review",
            "no_live_writes": True,
        },
    }
