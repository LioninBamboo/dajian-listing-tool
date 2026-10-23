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
from src.utils.publish_validation import (  # noqa: E402
    category_validation_errors,
    measurement_validation_errors,
)
from src.utils.title_sanitizer import normalize_listing_title_for_ebay, strip_supplier_brand_prefix  # noqa: E402
from src.utils.approved_category import (  # noqa: E402
    approved_category_id,
    blocks_automated_category_change,
    locked_category_write_error,
    plan_locked_override_revision,
)
from src.utils.category_decision import (  # noqa: E402
    category_gate_report_flags,
    explicit_override_target_allowed,
    gate_publish_category_choice,
)


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


def parse_explicit_targets(values, global_category_id=None):
    targets = {}
    for item in values or []:
        if "=" not in str(item):
            raise SystemExit(f"--target-category expects SKU=ID, got {item}")
        sku, cid = str(item).split("=", 1)
        sku, cid = sku.strip(), cid.strip()
        if sku and cid:
            targets[sku] = cid
    return targets, str(global_category_id or "").strip()


def print_readback_table(target_skus, confirmed_skus, unfinished_skus):
    print("readback: target / confirmed / unfinished")
    print(f"  target     ({len(target_skus)}): {', '.join(target_skus) or '-'}")
    print(f"  confirmed  ({len(confirmed_skus)}): {', '.join(confirmed_skus) or '-'}")
    print(f"  unfinished ({len(unfinished_skus)}): {', '.join(unfinished_skus) or '-'}")


def audit_candidate(
    product_row: dict,
    matcher,
    client,
    *,
    override_category_lock=False,
    explicit_targets=None,
    global_category_id="",
):
    opt = product_row["optimization"]
    title, description, existing_aspects, matcher_id, matcher_name, completed_aspects = compute_target(product_row, matcher)
    live_info = get_live_offer_info(client, product_row["sku"])
    local_category_id = normalize_category_id(opt.get("categoryId"))
    local_category_name = opt.get("categoryName")
    live_category_id = live_info["live_category_id"]
    live_category_name = live_info["live_category_name"]
    stored = raw_category_id(local_category_id) or raw_category_id(live_category_id) or ""
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
    explicit_target = (explicit_targets or {}).get(product_row["sku"]) or global_category_id
    override_plan = (
        plan_locked_override_revision(opt, live_category_id, explicit_target_id=explicit_target)
        if locked
        else {"action": "", "target_id": "", "reason": ""}
    )

    skip_reason = ""
    if locked:
        # Never use a matcher rematch for locked rows.
        target_id = override_plan["target_id"] or None
        target_name = None
        if override_category_lock and explicit_target and explicit_target == target_id:
            ok, reason = explicit_override_target_allowed(
                explicit_target, title, stored=stored
            )
            if not ok:
                action = "SKIP"
                skip_reason = reason
                target_id = explicit_target
            else:
                action = override_plan["action"] or "REVISE_LIVE"
                target_id = explicit_target
        else:
            action = "category_locked"
            skip_reason = "category_locked"
            target_id = None
    else:
        target_id = matcher_id
        target_name = matcher_name
        gated, gate_action, hold_err = gate_publish_category_choice(
            stored, target_id, title, "PUBLISHED", reason="publish_candidate"
        )
        if target_id and target_id != stored and gate_action != "remap":
            action = "would_remap_report_only"
            skip_reason = hold_err or "would_remap_report_only"
            target_id = matcher_id
        else:
            action = "OK"
            target_id = gated or stored or matcher_id

    final_category_id = target_id
    final_category_name = target_name
    live_matches_target = bool(target_id and live_category_id == target_id)
    live_canonical_matches_target = bool(target_id and canonical_live_category_id == target_id)
    needs_live_revision = bool(
        target_id
        and live_category_id
        and live_category_id != target_id
        and action not in {"category_locked", "would_remap_report_only", "SKIP"}
        and (live_canonical_matches_target or not live_plausible)
    )
    if action not in {"category_locked", "would_remap_report_only", "SKIP"}:
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
        "skip_reason": skip_reason,
        "action": action,
        "flags": category_gate_report_flags(stored, optimization=opt),
    }


def apply_live_revision(conn: sqlite3.Connection, matcher, client, product_row: dict, candidate: dict, *, override_category_lock=False):
    sku = product_row["sku"]
    opt = dict(product_row.get("optimization") or {})
    lock_error = locked_category_write_error(opt, override=override_category_lock)
    if lock_error:
        raise RuntimeError(lock_error)
    target = candidate.get("target_category_id")
    stored = opt.get("categoryId")
    title = candidate.get("title") or ""
    if str(target or "").strip() == str(stored or "").strip():
        pass
    elif override_category_lock:
        ok, reason = explicit_override_target_allowed(target, title, stored=stored)
        if not ok:
            raise RuntimeError(reason)
    else:
        gated, action, hold_err = gate_publish_category_choice(
            stored,
            target,
            title,
            "PUBLISHED",
            reason="publish_candidate",
            optimization=opt,
        )
        if hold_err or action != "remap":
            raise RuntimeError(hold_err or "would_remap_report_only")
    enrich_product_dimensions(product_row)

    opt["categoryId"] = candidate["target_category_id"]
    opt["categoryName"] = candidate["target_category_name"]
    opt["aspects"] = candidate["completed_aspects"]
    product_row["optimization"] = opt

    final_aspects = enrich_aspects(product_row)
    title_context = " ".join(part for part in ((product_row.get("title") or "").strip(), candidate["title"]) if part).strip()
    blockers = []
    blockers.extend(measurement_validation_errors(final_aspects))
    blockers.extend(category_validation_errors(matcher, title_context, candidate["target_category_id"], candidate["target_category_name"]))
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


def apply_db_sync(conn: sqlite3.Connection, product_row: dict, candidate: dict, *, override_category_lock=False):
    opt = dict(product_row.get("optimization") or {})
    lock_error = locked_category_write_error(opt, override=override_category_lock)
    if lock_error:
        raise RuntimeError(lock_error)
    target = candidate.get("final_category_id") or candidate.get("target_category_id")
    stored = opt.get("categoryId")
    title = candidate.get("title") or ""
    if str(target or "").strip() == str(stored or "").strip():
        pass
    elif override_category_lock:
        ok, reason = explicit_override_target_allowed(target, title, stored=stored)
        if not ok:
            raise RuntimeError(reason)
    else:
        gated, action, hold_err = gate_publish_category_choice(
            stored,
            target,
            title,
            "PUBLISHED",
            reason="publish_candidate",
            optimization=opt,
        )
        if hold_err or action != "remap":
            raise RuntimeError(hold_err or "would_remap_report_only")
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
        help="Allow changing a listing whose optimization.categoryApproved flag is set",
    )
    parser.add_argument(
        "--category-id",
        dest="category_id",
        help="Explicit override target id (never a matcher suggestion)",
    )
    parser.add_argument(
        "--target-category",
        action="append",
        default=[],
        help="Explicit per-SKU override as SKU=ID (repeatable)",
    )
    args = parser.parse_args()
    explicit_targets, global_category_id = parse_explicit_targets(
        args.target_category, args.category_id
    )

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
            global_category_id=global_category_id,
        )
        results.append(candidate)
        logger.info(
            "%s | action=%s | local=%s/%s | live=%s/%s | target=%s/%s",
            candidate["sku"],
            candidate["action"],
            candidate["local_category_id"],
            candidate["local_category_name"],
            candidate["live_category_id"],
            candidate["live_category_name"],
            candidate["target_category_id"],
            candidate["target_category_name"],
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
    applied_skus = {item.get("sku") for item in applied if item.get("action") not in {"ERROR", None}}
    target_skus = [
        item["sku"]
        for item in results
        if item.get("action") in {"REVISE_LIVE", "SYNC_DB", "would_remap_report_only", "category_locked"}
        or item.get("target_category_id")
    ]
    confirmed_skus = [
        item["sku"]
        for item in results
        if item.get("action") in {"OK", "REVISE_LIVE", "SYNC_DB"}
        and (not args.apply or item["sku"] in applied_skus or item.get("action") == "OK")
    ]
    if args.apply:
        confirmed_skus = [sku for sku in applied_skus] + [
            item["sku"] for item in results if item.get("action") == "OK"
        ]
    unfinished_skus = [
        item["sku"]
        for item in results
        if item.get("action") in {"category_locked", "would_remap_report_only", "SKIP", "ERROR"}
        or (args.apply and item.get("action") in {"REVISE_LIVE", "SYNC_DB"} and item["sku"] not in applied_skus)
    ]
    print_readback_table(target_skus, confirmed_skus, unfinished_skus)


if __name__ == "__main__":
    main()
