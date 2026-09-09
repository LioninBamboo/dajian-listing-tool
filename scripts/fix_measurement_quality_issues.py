#!/usr/bin/env python3
"""
Fix published listing measurement quality issues in bulk.

Targets:
  1. Description spec tables that still contain NOT AVAILABLE.
  2. Product/item weights that incorrectly mirror package weight.

Strategy:
  - Pull trusted package/product measurements from Dajian API in batches.
  - Prefer explicit product dimensions/weight from source description or assembled fields.
  - When product weight is untrusted and only package weight is available, remove it from
    item specifics and mark the description row as "Not specified" instead of keeping bad data.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name)
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
    elif getattr(_stream, "buffer", None) is not None:
        setattr(
            sys,
            _stream_name,
            io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace", line_buffering=True),
        )

from src.clients.dajian_client import DaJianClient
from src.clients.real_ebay_client import create_real_ebay_client
from scripts.audit_fix_active_listings import _fill_missing_required_aspects
from src.utils.dimension_helpers import (
    build_package_weight_and_size,
    extract_dajian_measurements,
    extract_numeric_inches,
    extract_numeric_lbs,
    insert_dimension_note_row,
    replace_description_measurements,
    replace_description_weight_placeholder_with_package_weight,
    source_description_marks_dimensions_unavailable,
)


DB_PATH = ROOT / "ebay_collection.db"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

SINGLE_VALUE_KEYS = {
    "Assembly Required",
    "Base Material",
    "Color",
    "Energy Star",
    "Item Height",
    "Item Length",
    "Item Weight",
    "Item Width",
    "Number of Lights",
    "Resistance Type",
    "Set Includes",
    "Tabletop Material",
    "Type",
    "Upholstery Fabric",
    "Voltage",
}


def parse_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return json.loads(value)
    return {}


def _numeric_from_aspect(aspects: Dict, key: str, parser) -> Optional[float]:
    value = aspects.get(key)
    if isinstance(value, list):
        value = value[0] if value else None
    return parser(value) if value else None


def _same_weight_candidate(attrs: Dict, specs: Dict, aspects: Dict) -> Tuple[bool, Optional[float]]:
    package_weight = extract_numeric_lbs(specs.get("Package Weight (lbs.)") or specs.get("Shipping Weight"))
    if package_weight is None:
        return False, None

    attr_weight = extract_numeric_lbs(
        attrs.get("Product Weight (lbs.)")
        or attrs.get("Product Weight")
        or attrs.get("Item Weight")
    )
    aspect_weight = _numeric_from_aspect(aspects, "Item Weight", extract_numeric_lbs)

    same_attr = attr_weight is not None and abs(attr_weight - package_weight) < 0.01
    same_aspect = aspect_weight is not None and abs(aspect_weight - package_weight) < 0.01
    return same_attr or same_aspect, package_weight


def _normalize_package_weight_and_size(pkg: Optional[Dict]) -> Optional[Dict]:
    if not pkg:
        return None
    normalized: Dict[str, Dict] = {}

    weight = pkg.get("weight") or {}
    weight_value = extract_numeric_lbs(weight.get("value"))
    if weight_value is not None:
        normalized["weight"] = {"value": round(weight_value, 2), "unit": weight.get("unit", "POUND")}

    dims = pkg.get("dimensions") or pkg.get("packageDimensions") or {}
    dim_values = {
        "length": extract_numeric_inches(dims.get("length")),
        "width": extract_numeric_inches(dims.get("width")),
        "height": extract_numeric_inches(dims.get("height")),
    }
    if all(value is not None for value in dim_values.values()):
        normalized["dimensions"] = {
            "length": round(dim_values["length"], 2),
            "width": round(dim_values["width"], 2),
            "height": round(dim_values["height"], 2),
            "unit": dims.get("unit", "INCH"),
        }

    shipping_irregular = pkg.get("shippingIrregular")
    if shipping_irregular is not None:
        normalized["shippingIrregular"] = bool(shipping_irregular)

    return normalized or None


def _live_package_conflicts_with_product(
    live_pkg: Optional[Dict],
    expected_pkg: Optional[Dict],
    safe_weight: Optional[float],
    dims: Tuple[Optional[float], Optional[float], Optional[float]],
) -> bool:
    live_pkg = _normalize_package_weight_and_size(live_pkg)
    expected_pkg = _normalize_package_weight_and_size(expected_pkg)
    if expected_pkg is not None:
        return live_pkg != expected_pkg
    if not live_pkg:
        return False

    live_weight = extract_numeric_lbs(((live_pkg.get("weight") or {}).get("value")))
    if live_weight is not None and safe_weight is not None and abs(live_weight - safe_weight) < 0.01:
        return True

    live_dims = live_pkg.get("dimensions") or {}
    product_length, product_width, product_height = dims
    if all(value is not None for value in (product_length, product_width, product_height)):
        dim_pairs = (
            (extract_numeric_inches(live_dims.get("length")), product_length),
            (extract_numeric_inches(live_dims.get("width")), product_width),
            (extract_numeric_inches(live_dims.get("height")), product_height),
        )
        if all(live_value is not None and abs(live_value - product_value) < 0.01 for live_value, product_value in dim_pairs):
            return True

    return False


def _resolve_dimensions(attrs: Dict, aspects: Dict, trusted: Dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    dims = [
        extract_numeric_inches(attrs.get("Assembled Length (in.)")),
        extract_numeric_inches(attrs.get("Assembled Width (in.)")),
        extract_numeric_inches(attrs.get("Assembled Height (in.)")),
    ]
    if all(v is not None for v in dims):
        return dims[0], dims[1], dims[2]

    dims = [
        trusted.get("assembledLength"),
        trusted.get("assembledWidth"),
        trusted.get("assembledHeight"),
    ]
    if all(v is not None for v in dims):
        return dims[0], dims[1], dims[2]

    dims = [
        _numeric_from_aspect(aspects, "Item Length", extract_numeric_inches),
        _numeric_from_aspect(aspects, "Item Width", extract_numeric_inches),
        _numeric_from_aspect(aspects, "Item Height", extract_numeric_inches),
    ]
    if all(v is not None for v in dims):
        return dims[0], dims[1], dims[2]

    return None, None, None


def _resolve_safe_weight(attrs: Dict, aspects: Dict, package_weight: Optional[float], trusted: Dict) -> Optional[float]:
    if trusted.get("productWeight") is not None:
        return trusted["productWeight"]

    candidates = [
        extract_numeric_lbs(attrs.get("Product Weight (lbs.)")),
        extract_numeric_lbs(attrs.get("Product Weight")),
        _numeric_from_aspect(aspects, "Item Weight", extract_numeric_lbs),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        if package_weight is not None and abs(candidate - package_weight) < 0.01:
            continue
        return candidate
    return None


def _should_upgrade_source_description(current_description: str, trusted: Dict) -> Optional[str]:
    trusted_description = trusted.get("description") or ""
    if not trusted_description:
        return None

    current_description = current_description or ""
    current_has_measurements = (
        "Product Information" in current_description
        or "Product Dimensions" in current_description
        or "Item Weight" in current_description
    )
    trusted_has_measurements = (
        "Product Information" in trusted_description
        or "Product Dimensions" in trusted_description
        or "Item Weight" in trusted_description
    )

    if trusted_has_measurements and (not current_has_measurements or len(trusted_description) > len(current_description)):
        return trusted_description
    return None


def _sanitize_aspects(aspects: Dict) -> Dict:
    cleaned = {}
    for key, value in (aspects or {}).items():
        if isinstance(value, list):
            values = [str(v).strip() for v in value if str(v).strip() and str(v).strip().upper() != "NOT AVAILABLE"]
        elif value is None:
            values = []
        else:
            text = str(value).strip()
            values = [text] if text and text.upper() != "NOT AVAILABLE" else []

        if not values:
            continue
        if key in SINGLE_VALUE_KEYS:
            cleaned[key] = [values[0]]
        else:
            cleaned[key] = values
    return cleaned


def _expand_sku_filter(sku_args: Optional[List[str]]) -> List[str]:
    expanded: List[str] = []
    for raw in sku_args or []:
        for part in raw.split(","):
            sku = part.strip()
            if sku and sku not in expanded:
                expanded.append(sku)
    return expanded


def load_issue_rows(conn: sqlite3.Connection, sku_filter: Optional[List[str]] = None) -> List[sqlite3.Row]:
    base_query = (
        "SELECT sku, title, description, attributes, specs, optimization, images, price, suggested_price, listing_id "
        "FROM collected_products WHERE status = 'PUBLISHED'"
    )
    if sku_filter:
        placeholders = ",".join("?" for _ in sku_filter)
        rows = conn.execute(
            f"{base_query} AND sku IN ({placeholders}) ORDER BY sku",
            tuple(sku_filter),
        ).fetchall()
    else:
        rows = conn.execute(f"{base_query} ORDER BY updated_at DESC").fetchall()

    issue_rows = []
    for row in rows:
        if sku_filter:
            issue_rows.append(row)
            continue
        attrs = parse_json(row["attributes"])
        specs = parse_json(row["specs"])
        opt = parse_json(row["optimization"])
        desc = opt.get("description", "") if isinstance(opt, dict) else ""
        same_weight, _ = _same_weight_candidate(attrs, specs, opt.get("aspects", {}))
        has_na = "Overall Dimensions" in desc and "NOT AVAILABLE" in desc.upper()
        if has_na or same_weight:
            issue_rows.append(row)
    return issue_rows


def fetch_dajian_details(client: DaJianClient, skus: List[str]) -> Dict[str, Dict]:
    details_by_sku: Dict[str, Dict] = {}
    for start in range(0, len(skus), 100):
        batch = skus[start:start + 100]
        details = client.get_product_details(batch)
        for detail in details:
            sku = detail.get("sku")
            if sku:
                details_by_sku[sku] = detail
        time.sleep(0.3)
    return details_by_sku


def main():
    parser = argparse.ArgumentParser(description="Fix published listing measurement quality issues.")
    parser.add_argument("--sku", action="append", help="Specific SKU(s) to fix. Repeat or pass comma-separated values.")
    args = parser.parse_args()
    sku_filter = _expand_sku_filter(args.sku)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    issue_rows = load_issue_rows(conn, sku_filter=sku_filter)
    if not issue_rows:
        print("No measurement issues detected.")
        return

    dajian = DaJianClient(os.getenv("DAJIAN_API_KEY"), os.getenv("DAJIAN_API_SECRET"))
    ebay = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    if not ebay.oauth.is_authorized():
        raise RuntimeError("eBay is not authorized")

    details_by_sku = fetch_dajian_details(dajian, [row["sku"] for row in issue_rows])

    summary = {
        "total_issue_rows": len(issue_rows),
        "updated": 0,
        "cleared_weight_candidates": 0,
        "filled_description_dimensions": 0,
        "marked_weight_unspecified": 0,
        "marked_dimensions_unspecified": 0,
        "refreshed_live_package_payload": 0,
        "errors": [],
        "results": [],
    }

    for idx, row in enumerate(issue_rows, start=1):
        sku = row["sku"]
        attrs = parse_json(row["attributes"])
        specs = parse_json(row["specs"])
        opt = parse_json(row["optimization"])
        aspects = dict(opt.get("aspects", {}))
        live_description = opt.get("description") or row["description"] or ""
        raw_description = row["description"] or ""

        trusted = extract_dajian_measurements(details_by_sku.get(sku) or {})
        same_weight, package_weight = _same_weight_candidate(attrs, specs, aspects)
        safe_weight = _resolve_safe_weight(attrs, aspects, package_weight, trusted)
        length, width, height = _resolve_dimensions(attrs, aspects, trusted)
        changed = False
        if source_description_marks_dimensions_unavailable(raw_description):
            length = width = height = None
            for key in ("Assembled Length (in.)", "Assembled Width (in.)", "Assembled Height (in.)"):
                if key in attrs:
                    attrs.pop(key, None)
                    changed = True
            for key in ("Item Length", "Item Width", "Item Height"):
                if key in aspects:
                    aspects.pop(key, None)
                    changed = True

        weight_cleared = False
        dimensions_filled = False
        weight_marked_unspecified = False
        dimensions_marked_unspecified = False
        live_package_refreshed = False

        # Keep package measurements in specs when Dajian returned them.
        spec_map = [
            ("length", "Package Length (in.)"),
            ("width", "Package Width (in.)"),
            ("height", "Package Height (in.)"),
            ("packageWeight", "Package Weight (lbs.)"),
        ]
        for source_key, spec_key in spec_map:
            source_value = trusted.get(source_key)
            if source_value is None:
                continue
            value_str = str(source_value)
            if specs.get(spec_key) != value_str:
                specs[spec_key] = value_str
                changed = True

        if length is not None and width is not None and height is not None:
            new_dim_values = {
                "Assembled Length (in.)": str(length),
                "Assembled Width (in.)": str(width),
                "Assembled Height (in.)": str(height),
            }
            for key, value in new_dim_values.items():
                if attrs.get(key) != value:
                    attrs[key] = value
                    changed = True
            aspect_dim_values = {
                "Item Length": [f"{length} in"],
                "Item Width": [f"{width} in"],
                "Item Height": [f"{height} in"],
            }
            for key, value in aspect_dim_values.items():
                if aspects.get(key) != value:
                    aspects[key] = value
                    changed = True

        if safe_weight is not None:
            target_weight = str(safe_weight)
            if attrs.get("Product Weight (lbs.)") != target_weight:
                attrs["Product Weight (lbs.)"] = target_weight
                changed = True
            if aspects.get("Item Weight") != [f"{safe_weight} lbs"]:
                aspects["Item Weight"] = [f"{safe_weight} lbs"]
                changed = True
        elif same_weight:
            for weight_key in ("Product Weight (lbs.)", "Product Weight", "Item Weight"):
                if weight_key in attrs:
                    attrs.pop(weight_key, None)
                    changed = True
            if "Item Weight" in aspects:
                aspects.pop("Item Weight", None)
                changed = True
                weight_cleared = True

        upgraded_source_description = _should_upgrade_source_description(raw_description, trusted)
        if upgraded_source_description and upgraded_source_description != raw_description:
            raw_description = upgraded_source_description
            changed = True

        has_na_description = "Overall Dimensions" in live_description and "NOT AVAILABLE" in live_description.upper()
        dimensions_text = None
        if length is not None and width is not None and height is not None:
            dimensions_text = f"{length} × {width} × {height} inches"
            if has_na_description:
                dimensions_filled = True
        elif has_na_description:
            dimensions_text = "Not specified"
            dimensions_marked_unspecified = True

        weight_text = None
        if safe_weight is not None:
            weight_text = f"{safe_weight} lbs"
            if has_na_description:
                weight_marked_unspecified = False
        elif same_weight or has_na_description:
            weight_text = "Not specified"
            weight_marked_unspecified = True

        updated_live_description = replace_description_measurements(
            live_description,
            length=length,
            width=width,
            height=height,
            weight=safe_weight,
            dimensions_text=dimensions_text,
            weight_text=weight_text,
        )
        needs_dimension_note = (
            length is not None
            and width is not None
            and height is not None
            and (
                ("Not Applicable" in raw_description or "NOT AVAILABLE" in raw_description.upper())
                and (
                    "组装长度" in raw_description
                    or "Overall Dimensions" in raw_description
                )
            )
        )
        if needs_dimension_note:
            updated_live_description = insert_dimension_note_row(
                updated_live_description,
                "See product dimension image for additional size reference.",
            )
        if safe_weight is None and package_weight is not None:
            updated_live_description = replace_description_weight_placeholder_with_package_weight(
                updated_live_description,
                package_weight,
            )

        if updated_live_description != live_description:
            opt["description"] = updated_live_description
            changed = True
        opt["aspects"] = aspects

        try:
            title = (opt.get("title") or row["title"] or "")[:80]
            category_id = str(opt.get("categoryId", ""))
            aspects = _fill_missing_required_aspects(aspects, category_id, title)
            aspects = _sanitize_aspects(aspects)
            opt["aspects"] = aspects

            db_images = json.loads(row["images"]) if row["images"] else []
            package_weight_and_size = build_package_weight_and_size(attrs, specs)
            inventory_item = ebay.get_inventory_item(sku) or {}
            live_image_urls = (inventory_item.get("product") or {}).get("imageUrls") or []
            # Never overwrite live imageUrls with raw GigaB2B signed URLs.
            # Reuse what eBay already hosts; only re-upload via EPS when the
            # live count is degraded vs the local source images.
            expected_count = min(len(db_images), 24)
            if live_image_urls and len(live_image_urls) >= max(1, expected_count - 1):
                image_urls_for_put = list(live_image_urls)
            elif db_images:
                image_urls_for_put = ebay.upload_images_to_eps(db_images, max_images=24) or list(live_image_urls)
            else:
                image_urls_for_put = list(live_image_urls)
            live_quantity = (
                ((inventory_item.get("availability") or {}).get("shipToLocationAvailability") or {})
                .get("quantity")
            )
            from src.utils.ebay_quantity import normalize_ebay_listing_quantity
            inv_product = {
                "title": title,
                "description": opt["description"],
                "image_urls": image_urls_for_put,
                "price": row["suggested_price"] or row["price"] or 99.99,
                "quantity": normalize_ebay_listing_quantity(live_quantity or 1),
                "condition": "NEW",
                "aspects": aspects,
            }
            live_package = inventory_item.get("packageWeightAndSize")
            if _live_package_conflicts_with_product(
                live_package,
                package_weight_and_size,
                safe_weight,
                (length, width, height),
            ):
                live_package_refreshed = True
                changed = True
            if package_weight_and_size:
                inv_product["packageWeightAndSize"] = package_weight_and_size

            if not changed:
                continue

            ebay.create_or_replace_inventory_item(sku=sku, product=inv_product)
            offers = ebay.get_offers_by_sku(sku)
            if offers:
                offer = offers[0]
                offer_updated = ebay.update_offer_category(
                    offer["offerId"],
                    category_id,
                    listing_description=opt["description"],
                )
                if not offer_updated:
                    raise RuntimeError(f"Offer update failed for {sku}")
            else:
                offer = {}

            conn.execute(
                "UPDATE collected_products SET attributes = ?, specs = ?, description = ?, optimization = ? WHERE sku = ?",
                (
                    json.dumps(attrs, ensure_ascii=False),
                    json.dumps(specs, ensure_ascii=False),
                    raw_description,
                    json.dumps(opt, ensure_ascii=False),
                    sku,
                ),
            )
            conn.commit()

            summary["updated"] += 1
            if weight_cleared:
                summary["cleared_weight_candidates"] += 1
            if dimensions_filled:
                summary["filled_description_dimensions"] += 1
            if weight_marked_unspecified:
                summary["marked_weight_unspecified"] += 1
            if dimensions_marked_unspecified:
                summary["marked_dimensions_unspecified"] += 1
            if live_package_refreshed:
                summary["refreshed_live_package_payload"] += 1

            summary["results"].append(
                {
                    "sku": sku,
                    "offerId": offer.get("offerId"),
                    "listingId": row["listing_id"],
                    "sameWeightCandidate": same_weight,
                    "safeWeight": safe_weight,
                    "dimensions": [length, width, height],
                    "weightCleared": weight_cleared,
                    "dimensionsText": dimensions_text,
                    "weightText": weight_text,
                    "livePackageRefreshed": live_package_refreshed,
                    "packageWeightAndSize": package_weight_and_size,
                }
            )
            print(f"[{idx}/{len(issue_rows)}] Fixed {sku}")
        except Exception as exc:
            conn.rollback()
            summary["errors"].append({"sku": sku, "error": str(exc)})
            print(f"[{idx}/{len(issue_rows)}] ERROR {sku}: {exc}")

    report_path = LOG_DIR / f"measurement_bulk_fix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), **{k: v for k, v in summary.items() if k != 'results'}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
