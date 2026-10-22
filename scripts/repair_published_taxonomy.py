#!/usr/bin/env python3
"""Repair published listings whose local or live categories are stale or implausible."""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from batch_publish import (  # noqa: E402
    enrich_aspects,
    enrich_product_dimensions,
)
from src.clients.real_ebay_client import create_real_ebay_client  # noqa: E402
from src.services.ebay_category_matcher import create_category_matcher  # noqa: E402
from src.services.taxonomy_constants import INVALID_CATEGORY_REMAP  # noqa: E402
from src.utils.dimension_helpers import build_package_weight_and_size  # noqa: E402
from src.utils.approved_category import (  # noqa: E402
    approved_category_id,
    blocks_automated_category_change,
    locked_category_write_error,
    plan_locked_override_revision,
)
from src.utils.publish_validation import (  # noqa: E402
    category_validation_errors,
    measurement_validation_errors,
)
from src.utils.title_sanitizer import normalize_listing_title_for_ebay, strip_supplier_brand_prefix  # noqa: E402


LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

STALE_LOCAL_CATEGORY_IDS = {
    "116364",
    "116365",
    "116366",
    "116379",
    "116391",
    "116394",
    "116403",
    "121856",
    "20497",
    "20751",
}
EXPLICIT_SKUS = {
    "N775P397559W",
    "W1422P412933",
    "W2500P283871",
    "W5709P462783",
}


def parse_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except Exception:
            return {}
    return {}


def normalize_category_id(value):
    category_id = str(value or "").strip()
    if not category_id:
        return None
    return INVALID_CATEGORY_REMAP.get(category_id, category_id)


def raw_category_id(value):
    category_id = str(value or "").strip()
    return category_id or None


def load_candidate_rows(conn: sqlite3.Connection, sku_filter: str = None):
    if sku_filter:
        rows = conn.execute(
            "SELECT * FROM collected_products WHERE status='PUBLISHED' AND sku=?",
            (sku_filter,),
        ).fetchall()
        return rows

    category_placeholders = ",".join("?" for _ in STALE_LOCAL_CATEGORY_IDS)
    sku_placeholders = ",".join("?" for _ in EXPLICIT_SKUS)
    query = (
        "SELECT * FROM collected_products "
        "WHERE status='PUBLISHED' AND ("
        f"json_extract(optimization, '$.categoryId') IN ({category_placeholders}) "
        f"OR sku IN ({sku_placeholders})"
        ") ORDER BY sku"
    )
    params = [*sorted(STALE_LOCAL_CATEGORY_IDS), *sorted(EXPLICIT_SKUS)]
    return conn.execute(query, params).fetchall()


def get_title_for_match(row_dict: dict, opt: dict) -> str:
    raw_title = (opt.get("title") or row_dict.get("title") or "").strip()
    title, _, _ = strip_supplier_brand_prefix(raw_title)
    return (title or row_dict.get("title") or raw_title or "").strip()


def compute_target(match_row: dict, matcher):
    opt = match_row["optimization"]
    title = get_title_for_match(match_row, opt)
    description = opt.get("description") or match_row.get("description") or ""
    aspects = dict(opt.get("aspects") or {})
    target_id, target_name, completed_aspects = matcher.get_category_and_aspects(title, aspects, description)
    target_id = normalize_category_id(target_id)
    if target_id:
        target_id, target_name = matcher.canonicalize_category(title, target_id, target_name, description)
    return title, description, aspects, target_id, target_name, completed_aspects


def parse_explicit_targets(values: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``SKU=categoryId`` override targets."""
    targets: dict[str, str] = {}
    for raw in values or []:
        text = str(raw or "").strip()
        if not text or "=" not in text:
            raise ValueError(f"invalid --target-category {raw!r}; expected SKU=categoryId")
        sku, category_id = text.split("=", 1)
        sku = sku.strip()
        category_id = str(category_id or "").strip()
        if not sku or not category_id:
            raise ValueError(f"invalid --target-category {raw!r}; expected SKU=categoryId")
        targets[sku] = category_id
    return targets


def parse_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    for field in ("optimization", "images", "specs", "attributes", "videos", "cost_breakdown", "logs"):
        data[field] = parse_json(data.get(field)) if field != "images" else parse_json(data.get(field)) or []
    if not isinstance(data.get("images"), list):
        data["images"] = []
    return data


def get_live_offer_info(client, sku: str) -> dict:
    offers = client.get_offers_by_sku(sku)
    offer = offers[0] if offers else {}
    category = offer.get("category") or {}
    return {
        "offer": offer,
        "offer_id": offer.get("offerId"),
        "live_category_id": raw_category_id(offer.get("categoryId") or category.get("categoryId")),
        "live_category_name": category.get("categoryName"),
    }


def update_local_optimization(conn: sqlite3.Connection, sku: str, opt: dict, category_id: str, category_name: str, aspects: dict = None):
    updated_opt = dict(opt or {})
    if category_id:
        updated_opt["categoryId"] = category_id
    if category_name:
        updated_opt["categoryName"] = category_name
    if aspects is not None:
        updated_opt["aspects"] = aspects
    conn.execute(
        "UPDATE collected_products SET optimization = ? WHERE sku = ?",
        (json.dumps(updated_opt, ensure_ascii=False), sku),
    )
    conn.commit()


def prepare_inventory_payload(product_row: dict, description: str, aspects: dict, required_aspect_names: list, inventory_item: dict):
    inventory_product = inventory_item.get("product") or {}
    availability = inventory_item.get("availability") or {}
    ship_to = availability.get("shipToLocationAvailability") or {}
    package_weight_and_size = inventory_item.get("packageWeightAndSize") or build_package_weight_and_size(
        product_row.get("attributes") or {},
        product_row.get("specs") or {},
    )
    title = get_title_for_match(product_row, product_row.get("optimization") or {})

    safe_title, _ = normalize_listing_title_for_ebay(title, source_title=product_row.get("title", ""))

    payload = {
        "title": safe_title,
        "description": description,
        "image_urls": inventory_product.get("imageUrls") or product_row.get("images") or [],
        "video_urls": inventory_product.get("videoIds") or [],
        "price": product_row.get("suggested_price") or product_row.get("price") or 99.99,
        "quantity": ship_to.get("quantity") or 1,
        "condition": inventory_item.get("condition") or "NEW",
        "aspects": aspects,
        "required_aspect_names": required_aspect_names,
    }
    if package_weight_and_size:
        payload["packageWeightAndSize"] = package_weight_and_size
    return payload


def audit_candidate(
    product_row: dict,
    matcher,
    client,
    *,
    override_category_lock: bool = False,
    explicit_targets: dict[str, str] | None = None,
):
    opt = product_row["optimization"]
    title, description, existing_aspects, matcher_id, matcher_name, matcher_aspects = compute_target(
        product_row, matcher
    )
    live_info = get_live_offer_info(client, product_row["sku"])
    local_category_id = normalize_category_id(opt.get("categoryId"))
    local_category_name = opt.get("categoryName")
    live_category_id = live_info["live_category_id"]
    live_category_name = live_info["live_category_name"]
    canonical_live_category_id = None
    canonical_live_category_name = live_category_name
    if live_category_id:
        canonical_live_category_id, canonical_live_category_name = matcher.canonicalize_category(
            title,
            live_category_id,
            live_category_name,
            description,
        )

    live_plausible = bool(
        live_category_id
        and live_category_id == canonical_live_category_id
        and matcher.is_category_plausible_for_text(title, live_category_id, live_category_name)
    )

    locked = blocks_automated_category_change(opt)
    explicit_target = (explicit_targets or {}).get(product_row["sku"])
    override_plan = plan_locked_override_revision(
        opt,
        live_category_id,
        explicit_target_id=explicit_target,
    ) if locked else {"action": "", "target_id": "", "reason": ""}

    if locked:
        # Never use a fresh matcher rematch for locked rows.
        target_id = override_plan["target_id"] or None
        stored_leaf = str(opt.get("categoryId") or "").strip()
        if target_id and stored_leaf == target_id:
            target_name = local_category_name or f"Category {target_id}"
        elif target_id and live_category_id == target_id and live_category_name:
            target_name = live_category_name
        elif target_id:
            target_name = f"Category {target_id}"
        else:
            target_name = None
        completed_aspects = existing_aspects
        final_category_id = target_id or live_category_id or local_category_id
        final_category_name = target_name or live_category_name or local_category_name
        needs_live_revision = override_plan["action"] == "REVISE_LIVE"
        needs_db_sync = bool(override_category_lock and override_plan["action"] == "SYNC_DB")
        action = "LOCKED"
        if override_category_lock:
            action = override_plan["action"]
    else:
        target_id = matcher_id
        target_name = matcher_name
        completed_aspects = matcher_aspects
        final_category_id = target_id
        final_category_name = target_name
        live_matches_target = bool(target_id and live_category_id == target_id)
        live_canonical_matches_target = bool(target_id and canonical_live_category_id == target_id)
        needs_live_revision = bool(
            target_id
            and live_category_id
            and live_category_id != target_id
            and (live_canonical_matches_target or not live_plausible)
        )
        if live_category_id and live_plausible and live_category_id != target_id and not needs_live_revision:
            final_category_id = live_category_id
            final_category_name = live_category_name or target_name
        elif live_matches_target:
            final_category_id = live_category_id
            final_category_name = live_category_name or target_name
        needs_db_sync = bool(
            final_category_id and (
                local_category_id != final_category_id
                or (final_category_name and local_category_name != final_category_name)
            )
        )
        action = "OK"
        if needs_live_revision:
            action = "REVISE_LIVE"
        elif needs_db_sync:
            action = "SYNC_DB"

    return {
        "sku": product_row["sku"],
        "listing_id": product_row.get("listing_id"),
        "offer_id": live_info["offer_id"],
        "title": title,
        "description": description,
        "existing_aspects": existing_aspects,
        "target_category_id": target_id,
        "target_category_name": target_name,
        "completed_aspects": completed_aspects,
        "local_category_id": local_category_id,
        "local_category_name": local_category_name,
        "live_category_id": live_category_id,
        "live_category_name": live_category_name,
        "canonical_live_category_id": canonical_live_category_id,
        "canonical_live_category_name": canonical_live_category_name,
        "live_plausible": live_plausible,
        "final_category_id": final_category_id,
        "final_category_name": final_category_name,
        "approved_category_id": approved_category_id(opt),
        "override_action": override_plan["action"] or None,
        "override_reason": override_plan["reason"] or None,
        "skip_canonicalize": locked,
        "action": action,
    }


def apply_live_revision(
    conn: sqlite3.Connection,
    matcher,
    client,
    product_row: dict,
    candidate: dict,
    *,
    override_category_lock: bool = False,
):
    sku = product_row["sku"]
    lock_error = locked_category_write_error(
        product_row.get("optimization") or {},
        override=override_category_lock,
    )
    if lock_error:
        raise RuntimeError(lock_error)
    enrich_product_dimensions(product_row)

    opt = dict(product_row.get("optimization") or {})
    opt["categoryId"] = candidate["target_category_id"]
    opt["categoryName"] = candidate["target_category_name"]
    opt["aspects"] = candidate["completed_aspects"]
    product_row["optimization"] = opt

    final_aspects = enrich_aspects(product_row)
    title_context = " ".join(part for part in ((product_row.get("title") or "").strip(), candidate["title"]) if part).strip()
    blockers = []
    blockers.extend(measurement_validation_errors(final_aspects))
    blockers.extend(
        category_validation_errors(
            matcher,
            title_context,
            candidate["target_category_id"],
            candidate["target_category_name"],
            skip_canonicalize=bool(candidate.get("skip_canonicalize")),
        )
    )
    if blockers:
        raise RuntimeError("; ".join(blockers))

    inventory_item = client.get_inventory_item(sku) or {}
    required_aspects, _ = matcher._get_category_aspects(candidate["target_category_id"])
    required_aspect_names = [a.get("name") for a in required_aspects if a.get("name")]
    payload = prepare_inventory_payload(
        product_row,
        candidate["description"],
        final_aspects,
        required_aspect_names,
        inventory_item,
    )
    client.create_or_replace_inventory_item(sku=sku, product=payload)

    offer_id = candidate["offer_id"]
    if not offer_id:
        raise RuntimeError("missing live offerId")
    if not client.update_offer_category(offer_id, candidate["target_category_id"], listing_description=candidate["description"]):
        raise RuntimeError(f"offer update failed for {offer_id}")

    update_local_optimization(
        conn,
        sku,
        opt,
        candidate["target_category_id"],
        candidate["target_category_name"],
        final_aspects,
    )
    return {
        "sku": sku,
        "action": "REVISED",
        "offer_id": offer_id,
        "listing_id": product_row.get("listing_id"),
        "from_live": candidate["live_category_id"],
        "to_live": candidate["target_category_id"],
    }


def apply_db_sync(
    conn: sqlite3.Connection,
    product_row: dict,
    candidate: dict,
    *,
    override_category_lock: bool = False,
):
    lock_error = locked_category_write_error(
        product_row.get("optimization") or {},
        override=override_category_lock,
    )
    if lock_error:
        raise RuntimeError(lock_error)
    update_local_optimization(
        conn,
        product_row["sku"],
        product_row.get("optimization") or {},
        candidate["final_category_id"],
        candidate["final_category_name"],
    )
    return {
        "sku": product_row["sku"],
        "action": "SYNCED_DB",
        "listing_id": product_row.get("listing_id"),
        "category_id": candidate["final_category_id"],
    }


def main():
    parser = argparse.ArgumentParser(description="Repair stale taxonomy for published listings")
    parser.add_argument("--apply", action="store_true", help="Apply live and local fixes")
    parser.add_argument("--sku", type=str, help="Limit to one published SKU")
    parser.add_argument(
        "--override-category-lock",
        action="store_true",
        help="Allow revising a listing whose optimization.categoryApproved flag is set",
    )
    parser.add_argument(
        "--target-category",
        action="append",
        default=[],
        metavar="SKU=ID",
        help="Explicit per-SKU lock override target (repeatable). Never inferred from the matcher.",
    )
    args = parser.parse_args()
    try:
        explicit_targets = parse_explicit_targets(args.target_category)
    except ValueError as exc:
        parser.error(str(exc))

    conn = sqlite3.connect(str(ROOT / "ebay_collection.db"), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    matcher = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    rows = [parse_row(row) for row in load_candidate_rows(conn, args.sku)]
    results = []

    logger.info("Published taxonomy repair scan: %s candidates", len(rows))
    for row in rows:
        candidate = audit_candidate(
            row,
            matcher,
            client,
            override_category_lock=args.override_category_lock,
            explicit_targets=explicit_targets,
        )
        results.append(candidate)
        logger.info(
            "%s | action=%s | local=%s/%s | live=%s/%s | target=%s/%s | override=%s (%s)",
            candidate["sku"],
            candidate["action"],
            candidate["local_category_id"],
            candidate["local_category_name"],
            candidate["live_category_id"],
            candidate["live_category_name"],
            candidate["target_category_id"],
            candidate["target_category_name"],
            candidate.get("override_action") or "-",
            candidate.get("override_reason") or "n/a",
        )

    applied = []
    if args.apply:
        for row, candidate in zip(rows, results):
            try:
                if candidate["action"] == "REVISE_LIVE":
                    applied.append(
                        apply_live_revision(
                            conn,
                            matcher,
                            client,
                            row,
                            candidate,
                            override_category_lock=args.override_category_lock,
                        )
                    )
                elif candidate["action"] == "SKIP":
                    applied.append({
                        "sku": candidate["sku"],
                        "action": "SKIP",
                        "message": candidate.get("override_reason") or "skipped locked listing",
                    })
                elif candidate["action"] == "SYNC_DB":
                    applied.append(
                        apply_db_sync(
                            conn,
                            row,
                            candidate,
                            override_category_lock=args.override_category_lock,
                        )
                    )
                time.sleep(1.0)
            except Exception as exc:
                logger.error("%s failed: %s", candidate["sku"], exc)
                applied.append({
                    "sku": candidate["sku"],
                    "action": "ERROR",
                    "message": str(exc),
                })

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "apply": args.apply,
        "scan": results,
        "applied": applied,
    }
    report_path = LOG_DIR / f"repair_published_taxonomy_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Report written to %s", report_path)


if __name__ == "__main__":
    main()
