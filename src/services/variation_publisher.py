"""Publish a blind-box series as ONE eBay multi-variation listing (B5).

A blind-box series is a multi-variation listing: one variation per figure, each
with its own price/image, sharing a title/description. eBay's Inventory API models
this as an *inventory item group*:

  1. create an inventory item per variant SKU (each carries the varying aspect's
     value + its own image);
  2. create an (unpublished) offer per variant SKU (price/policies/location);
  3. PUT the inventory_item_group (varies-by aspect + values + variant SKUs);
  4. publish_by_inventory_item_group -> a single multi-variation listing.

This module holds the pure payload/derivation helpers (unit-tested) plus the
``publish_variation_group`` orchestration over a RealEbayClient.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _clean_value(text: str) -> str:
    v = re.sub(r"\s+", " ", str(text or "")).strip()
    return v[:50]


def derive_variation_values(
    variations: List[Dict[str, Any]],
    *,
    aspect_name: str = "Style",
    label_key: Optional[str] = None,
) -> List[str]:
    """Give each variation a DISTINCT value for the varying aspect.

    eBay requires every variant to have a unique varies-by value. If ``label_key``
    is given and yields distinct non-empty values across variations, use them;
    otherwise fall back to synthetic ``"<aspect> N"`` labels. Collisions are
    de-duplicated by suffixing an index so the result is always unique.
    """
    raw: List[str] = []
    if label_key:
        for v in variations:
            attrs = v.get("attributes") or {}
            val = attrs.get(label_key)
            if isinstance(val, (list, tuple)):
                val = val[0] if val else ""
            raw.append(_clean_value(val))
    else:
        raw = ["" for _ in variations]

    # Fall back to synthetic labels when source values are missing or not distinct.
    distinct = {r for r in raw if r}
    if len(distinct) < len(variations):
        raw = [f"{aspect_name} {i + 1}" for i in range(len(variations))]

    seen: Dict[str, int] = {}
    out: List[str] = []
    for r in raw:
        if r in seen:
            seen[r] += 1
            out.append(f"{r} ({seen[r]})")
        else:
            seen[r] = 1
            out.append(r)
    return out


def build_inventory_item_group_payload(
    *,
    title: str,
    description: str,
    image_urls: List[str],
    common_aspects: Dict[str, List[str]],
    varies_by_aspect: str,
    variant_skus: List[str],
    variant_values: List[str],
) -> Dict[str, Any]:
    """Build the inventory_item_group PUT payload.

    The varying aspect must NOT also live in common_aspects (eBay rejects that),
    so it is stripped here defensively.
    """
    if len(variant_skus) != len(variant_values):
        raise ValueError("variant_skus and variant_values must be the same length")
    # eBay rejects multi-valued single-value aspects (e.g. Theme/Type/Material) at
    # publish; cap them to their first value, mirroring inventory-item sanitization.
    try:
        from src.utils.publish_autofix import SINGLE_VALUE_ASPECTS
    except Exception:
        SINGLE_VALUE_ASPECTS = frozenset()
    common: Dict[str, List[str]] = {}
    for k, v in (common_aspects or {}).items():
        if k == varies_by_aspect:
            continue
        vals = list(v) if isinstance(v, (list, tuple)) else [v]
        if k in SINGLE_VALUE_ASPECTS and len(vals) > 1:
            vals = vals[:1]
        common[k] = vals
    return {
        "title": str(title or "")[:80],
        "description": description or "",
        "imageUrls": list(image_urls or [])[:12],
        "aspects": common,
        "variesBy": {
            "aspectsImageVariesBy": [varies_by_aspect],
            "specifications": [{"name": varies_by_aspect, "values": list(variant_values)}],
        },
        "variantSKUs": list(variant_skus),
    }


def publish_variation_group(
    client: Any,
    *,
    group_key: str,
    title: str,
    description: str,
    image_urls: List[str],
    common_aspects: Dict[str, List[str]],
    varies_by_aspect: str,
    variants: List[Dict[str, Any]],
    category_id: Optional[str] = None,
    default_quantity: int = 5,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Publish ``variants`` as one multi-variation listing.

    ``variants``: [{sku, value, price, aspects?, image_urls?, description?, quantity?}].
    ``dry_run`` builds every payload and returns them WITHOUT any eBay write — used
    to preview the exact listing before a human approves the real publish.
    """
    if not variants:
        raise ValueError("no variants to publish")

    variant_skus = [v["sku"] for v in variants]
    variant_values = [v["value"] for v in variants]
    built_items: List[Dict[str, Any]] = []

    for v in variants:
        item_aspects: Dict[str, List[str]] = {
            k: (list(val) if isinstance(val, (list, tuple)) else [str(val)])
            for k, val in (common_aspects or {}).items()
            if k != varies_by_aspect
        }
        for k, val in (v.get("aspects") or {}).items():
            item_aspects[k] = list(val) if isinstance(val, (list, tuple)) else [str(val)]
        item_aspects[varies_by_aspect] = [v["value"]]  # the varying value wins

        product = {
            "title": title,
            "description": v.get("description") or description,
            "image_urls": v.get("image_urls") or image_urls,
            "aspects": item_aspects,
            "condition": v.get("condition", "NEW"),
            "quantity": int(v.get("quantity", default_quantity)),
            "price": v["price"],
        }
        built_items.append({"sku": v["sku"], "product": product, "price": v["price"]})
        if not dry_run:
            client.create_or_replace_inventory_item(v["sku"], product)
            client.create_offer(
                v["sku"], v["price"], category_id=category_id,
                listing_description=v.get("description") or description,
            )

    group_payload = build_inventory_item_group_payload(
        title=title, description=description, image_urls=image_urls,
        common_aspects=common_aspects, varies_by_aspect=varies_by_aspect,
        variant_skus=variant_skus, variant_values=variant_values,
    )

    if dry_run:
        return {
            "dry_run": True,
            "group_key": group_key,
            "variant_count": len(variants),
            "group_payload": group_payload,
            "inventory_items": built_items,
        }

    client.create_or_replace_inventory_item_group(group_key, group_payload)
    result = client.publish_by_inventory_item_group(group_key)
    return {"dry_run": False, "group_key": group_key, "variant_count": len(variants), **result}
