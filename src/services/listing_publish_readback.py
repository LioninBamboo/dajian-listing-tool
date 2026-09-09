"""Pure readback verification for eBay Inventory and Offer writes.

The eBay API can return an error after a replacement-style write has changed
some fields.  This module deliberately performs no writes; it classifies the
post-write observations so callers can stop, compensate, or safely record a
verified result.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


LIVE_OFFER_STATUSES = frozenset({"PUBLISHED", "ACTIVE"})
LIVE_LISTING_STATUSES = frozenset({"ACTIVE", "PUBLISHED"})


def _issue(code: str, message: str, *, expected: Any = None, actual: Any = None) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "expected": expected,
        "actual": actual,
    }


def _nested(value: Mapping[str, Any] | None, *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first(value: Mapping[str, Any] | None, *paths: tuple[str, ...]) -> Any:
    for path in paths:
        candidate = _nested(value, *path)
        if candidate is not None:
            return candidate
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _canonical_value(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(name): _canonical_value(item, key=str(name))
            for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if key in {"value", "quantity"} and isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return value
        return int(numeric) if numeric.is_integer() else numeric
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _normalized_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _project_to_expected_shape(actual: Any, expected: Any) -> Any:
    """Ignore server-added mapping keys while retaining expected-value checks."""
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        return {
            key: _project_to_expected_shape(actual.get(key), expected_value)
            for key, expected_value in expected.items()
        }
    return actual


def _quantity(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_publish_readback_expectation(
    *,
    sku: str,
    inventory_product: Mapping[str, Any],
    category_id: str | None,
    offer_id: str | None,
    listing_id: str | None = None,
) -> dict[str, Any]:
    """Build an auditable expectation from the exact inventory write input."""

    product = dict(inventory_product or {})
    return {
        "sku": str(sku or ""),
        "title": product.get("title"),
        "description": product.get("description"),
        "image_urls": _as_list(product.get("image_urls")),
        "video_ids": _as_list(product.get("video_ids") or product.get("video_urls")),
        "quantity": _quantity(product.get("quantity")),
        "condition": product.get("condition", "NEW"),
        "package_weight_and_size": product.get("packageWeightAndSize"),
        "category_id": str(category_id or "") or None,
        "offer_id": str(offer_id or "") or None,
        "listing_id": str(listing_id or "") or None,
    }


def verify_publish_readback(
    expected: Mapping[str, Any],
    inventory_item: Mapping[str, Any] | None,
    offer: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify readback as verified, mismatch, partial write, or transport failure."""

    expected = dict(expected or {})
    issues: list[dict[str, Any]] = []
    transport_failures: list[dict[str, Any]] = []

    if not isinstance(inventory_item, Mapping):
        transport_failures.append(
            _issue("inventory_readback_missing", "Inventory readback missing")
        )
    if not isinstance(offer, Mapping):
        transport_failures.append(_issue("offer_readback_missing", "Offer readback missing"))

    live_product = _nested(inventory_item, "product")
    if isinstance(inventory_item, Mapping):
        expected_condition = str(expected.get("condition") or "").strip().upper()
        actual_condition = str(inventory_item.get("condition") or "").strip().upper()
        if expected_condition and actual_condition != expected_condition:
            issues.append(
                _issue(
                    "condition_mismatch",
                    "Inventory condition differs from the write expectation",
                    expected=expected_condition,
                    actual=actual_condition,
                )
            )

        expected_quantity = expected.get("quantity")
        actual_quantity = _quantity(
            _first(
                inventory_item,
                ("availability", "shipToLocationAvailability", "quantity"),
                ("availability", "pickupAtLocationAvailability", "quantity"),
            )
        )
        if expected_quantity is not None and actual_quantity != expected_quantity:
            issues.append(
                _issue(
                    "quantity_mismatch",
                    "Inventory quantity differs from the write expectation",
                    expected=expected_quantity,
                    actual=actual_quantity,
                )
            )

        expected_title = str(expected.get("title") or "").strip()
        actual_title = str(_nested(live_product, "title") or "").strip()
        if expected_title and actual_title != expected_title:
            issues.append(
                _issue(
                    "title_mismatch",
                    "Inventory title differs from the write expectation",
                    expected=expected_title,
                    actual=actual_title,
                )
            )

        expected_images = _as_list(expected.get("image_urls"))
        actual_images = _as_list(_nested(live_product, "imageUrls"))
        if expected_images and len(actual_images) < 2:
            issues.append(
                _issue(
                    "image_urls_collapsed",
                    "Inventory imageUrls collapsed below the safe minimum",
                    expected=len(expected_images),
                    actual=len(actual_images),
                )
            )
        elif expected_images and len(actual_images) < len(expected_images):
            issues.append(
                _issue(
                    "image_urls_truncated",
                    "Inventory imageUrls are fewer than the write expectation",
                    expected=len(expected_images),
                    actual=len(actual_images),
                )
            )

        expected_videos = {str(value) for value in _as_list(expected.get("video_ids")) if str(value)}
        actual_videos = {str(value) for value in _as_list(_nested(live_product, "videoIds")) if str(value)}
        if expected_videos and not expected_videos <= actual_videos:
            issues.append(
                _issue(
                    "video_ids_mismatch",
                    "Inventory videoIds differ from the write expectation",
                    expected=sorted(expected_videos),
                    actual=sorted(actual_videos),
                )
            )

        expected_package = expected.get("package_weight_and_size")
        if expected_package:
            # Inventory API returns packageWeightAndSize beside ``product``.
            # Keep the nested fallback for legacy fixtures/clients.
            actual_package = _first(
                inventory_item,
                ("packageWeightAndSize",),
                ("product", "packageWeightAndSize"),
            )
            comparable_actual_package = _project_to_expected_shape(
                actual_package,
                expected_package,
            )
            if _normalized_json(comparable_actual_package) != _normalized_json(expected_package):
                issues.append(
                    _issue(
                        "package_weight_and_size_mismatch",
                        "Inventory packageWeightAndSize differs from the write expectation",
                        expected=expected_package,
                        actual=actual_package,
                    )
                )

    if isinstance(offer, Mapping):
        expected_offer_id = str(expected.get("offer_id") or "")
        actual_offer_id = str(offer.get("offerId") or "")
        if expected_offer_id and actual_offer_id != expected_offer_id:
            issues.append(
                _issue(
                    "offer_id_mismatch",
                    "Offer readback identity differs from the created offer",
                    expected=expected_offer_id,
                    actual=actual_offer_id,
                )
            )

        expected_category = str(expected.get("category_id") or "")
        actual_category = str(
            _first(offer, ("categoryId",), ("category", "categoryId")) or ""
        )
        if expected_category and actual_category != expected_category:
            issues.append(
                _issue(
                    "category_id_mismatch",
                    "Offer category differs from the publish expectation",
                    expected=expected_category,
                    actual=actual_category,
                )
            )

        expected_listing_id = str(expected.get("listing_id") or "")
        actual_listing_id = str(
            _first(offer, ("listing", "listingId"), ("listingId",)) or ""
        )
        if expected_listing_id and actual_listing_id != expected_listing_id:
            issues.append(
                _issue(
                    "listing_id_mismatch",
                    "Offer listing ID differs from the publish result",
                    expected=expected_listing_id,
                    actual=actual_listing_id,
                )
            )

        if expected_listing_id:
            offer_status = str(offer.get("status") or "").strip().upper()
            listing_status = str(_nested(offer, "listing", "listingStatus") or "").strip().upper()
            if offer_status not in LIVE_OFFER_STATUSES and listing_status not in LIVE_LISTING_STATUSES:
                issues.append(
                    _issue(
                        "offer_not_live",
                        "Offer/listing readback is not active or published",
                        expected=sorted(LIVE_OFFER_STATUSES | LIVE_LISTING_STATUSES),
                        actual={"offer_status": offer_status, "listing_status": listing_status},
                    )
                )

    if transport_failures:
        status = "transport_failure"
    elif any(item["code"] == "image_urls_collapsed" for item in issues):
        status = "partial_write"
    elif issues:
        status = "mismatch"
    else:
        status = "verified"

    return {
        "status": status,
        "passed": status == "verified",
        "issues": issues,
        "transport_failures": transport_failures,
        "sku": expected.get("sku", ""),
        "offer_id": expected.get("offer_id"),
        "listing_id": expected.get("listing_id"),
    }
