#!/usr/bin/env python3
"""
Comprehensive audit & fix for ALL active eBay listings.

Checks:
  1. Category correctness (does category match product type keywords?)
  2. Dimension accuracy (do Item Length/Width/Height match source attributes?)
  3. Non-applicable aspects (e.g., Compatible Mattress Size on a safe)
  4. Description dimension consistency
  5. Weight correctness

Fixes issues by updating eBay listing via Inventory API + Offer API.
Also updates the local database optimization record.

Usage:
  python scripts/audit_fix_active_listings.py                # Audit only (dry run)
  python scripts/audit_fix_active_listings.py --fix           # Audit + fix on eBay
  python scripts/audit_fix_active_listings.py --fix --sku W1779P440173  # Fix single SKU
"""

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import io
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Force UTF-8 output on Windows without invalidating the underlying stream.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "ebay_collection.db"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

QUALITY_GATE_META_KEY = "_quality_gate"
QUALITY_GATE_META_VERSION = 1
QUALITY_GATE_RULESET_VERSION = 1
TRANSPORT_ISSUE_TYPES = {
    "live_fetch_failed",
    "live_inventory_missing",
    "live_offer_missing",
}


# ── Dimension extraction (reuse from dimension_helpers) ──
from src.utils.dimension_helpers import (
    extract_all_dimensions,
    extract_product_weight_from_text,
    find_weight,
    find_dimension,
    replace_description_measurements,
)
from src.utils.publish_validation import first_aspect_text, measurement_issue
from src.utils.publish_autofix import (
    SINGLE_VALUE_ASPECTS,
    prepare_ebay_aspects,
    sanitize_single_value_aspects,
)
from src.utils.html_truncator import smart_truncate_html
from src.utils.listing_quality_gate import (
    classify_listing_profile,
    find_assembly_description_contradictions,
    has_expected_assembly_copy,
    infer_source_assembly_required,
    infer_source_assembly_status,
    rewrite_assembly_copy,
    sanitize_generated_description_html,
)
from src.services.vehicle_compatibility import (
    EBAY_MOTORS_CATEGORIES,
    analyze_ebay_motors_compatibility,
    apply_compatibility_aspects,
    serialize_compatibility_analysis,
)
from src.services.ebay_category_matcher import create_category_matcher
from src.utils.publish_aspect_completion import infer_number_of_items_in_set
from src.utils.claim_diff_engine import (
    COUNTABLE_CLAIMS,
    FEATURE_CLAIM_PATTERNS,
    build_source_constraints,
    detect_claim_violations,
)
from src.utils.title_sanitizer import (
    normalize_listing_title_for_ebay,
    sanitize_listing_title,
    title_has_incomplete_trailing_fragment,
)


_CATEGORY_MATCHER = None
_FACT_SHEET_CONN = None


def get_category_matcher():
    global _CATEGORY_MATCHER
    if _CATEGORY_MATCHER is None:
        _CATEGORY_MATCHER = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    return _CATEGORY_MATCHER


def get_fact_sheet_conn():
    """Dedicated connection for the fact-sheet cache (semantic guard layer)."""
    global _FACT_SHEET_CONN
    if _FACT_SHEET_CONN is None:
        _FACT_SHEET_CONN = sqlite3.connect(DB_PATH)
    return _FACT_SHEET_CONN


# ── Non-applicable aspect rules ──
BED_KEYWORDS = {"bed", "bunk", "daybed", "mattress", "headboard", "bed frame", "platform bed"}
SAFE_KEYWORDS = {"safe", "gun safe", "security", "lockbox"}

NON_APPLICABLE_RULES = {
    "Compatible Mattress Size": lambda t: not any(kw in t for kw in BED_KEYWORDS),
    "For Gun Type": lambda t: not any(kw in t for kw in SAFE_KEYWORDS),
    "Firmness": lambda t: "mattress" not in t,
    "Seating Capacity": lambda t: not any(kw in t for kw in ["sofa", "chair", "bench", "couch", "dining"]),
}

WOOD_SPECIES_PATTERNS = {
    "Acacia Wood": (r"\bacacia\b",),
    "Teak Wood": (r"\bteak\b",),
    "Oak Wood": (r"\boak\b",),
    "Pine Wood": (r"\bpine\b",),
    "Rubberwood": (r"\brubber\s*wood\b", r"\brubberwood\b"),
    "Walnut Wood": (r"\bwalnut\b",),
    "Bamboo": (r"\bbamboo\b",),
    "Eucalyptus Wood": (r"\beucalyptus\b",),
}

WEATHER_RESISTANCE_PATTERNS = (
    r"\buv\s*[- ]?\s*resistant\b",
    r"\bwater\s*[- ]?\s*resistant\b",
    r"\bweather\s*[- ]?\s*resistant\b",
    r"\ball[- ]weather\b",
    r"\bfade\s*[- ]?\s*resistant\b",
    r"\bwaterproof\b",
)

CUSHION_PATTERNS = (
    r"\bcushions?\b",
    r"\bcushioned\b",
    r"\bpillow(?:s)?\b",
    r"\bseat\s+pad(?:s)?\b",
    r"\bupholster(?:ed|y)\b",
)

INHERENT_CUSHION_CONTEXT_PATTERNS = (
    r"\bsofa\b",
    r"\bloveseat\b",
    r"\bsectional\b",
    r"\brecliner\b",
    r"\barmchair\b",
    r"\bupholster(?:ed|y)\b",
    r"\bchenille\b",
    r"\bvelvet\b",
    r"\blinen\b",
    r"\bcorduroy\b",
    r"\bfaux\s+leather\b",
    r"\bpu\s+leather\b",
    r"\bleather\b",
    r"\bfoam\b",
    r"\bbean\s+bag\b",
)

POSITION_COUNT_PATTERNS = (
    r"\b(?:[2-9]|10|two|three|four|five|six|seven|eight|nine|ten)[-\s]+position(?:al)?\s+(?:backrest|recline|reclining|tilt|seat\s+back)\b",
    r"\b(?:[2-9]|10|two|three|four|five|six|seven|eight|nine|ten)[-\s]+level\s+(?:backrest|recline|reclining|tilt|seat\s+back)\b",
    r"\b(?:backrest|recline|reclining|tilt|seat\s+back)\s+with\s+(?:[2-9]|10|two|three|four|five|six|seven|eight|nine|ten)[-\s]+(?:position|level)s?\b",
)

COUNTABLE_CLAIM_FIX_PATTERNS = {
    "position": r"(?:position(?:al)?|positions|level|levels)",
    "tier": r"(?:tier|tiers)",
    "shelf": r"(?:shelf|shelves)",
    "drawer": r"(?:drawer|drawers)",
    "door": r"(?:door|doors)",
    "seat": r"(?:seat|seats|seater|seaters|seating)",
}

CLAIM_SPECIFIC_ASPECT_KEY_PATTERNS = {
    "foldable": (
        r"\bfolding\b",
        r"\bfoldable\b",
        r"\bcollapsible\b",
    ),
    "waterproof": (
        r"\bwaterproof\b",
        r"\bwater\s+resistance\b",
        r"\bwater\s+resistant\b",
    ),
    "zippered_floor": (
        r"\bclosure\s+type\b",
    ),
    "massage": (
        r"\bmassage\s+functions?\b",
        r"\bmassage\s+mode(?:s)?\b",
    ),
}


def parse_json(val):
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip():
        try:
            return json.loads(val)
        except Exception:
            return {}
    return {}


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


def replace_specifications_table_html(original_html: str, rebuilt_table_html: str) -> str:
    """Replace the existing SPECIFICATIONS block while preserving trailing sections."""
    if not original_html:
        return rebuilt_table_html
    if not rebuilt_table_html:
        return original_html

    patterns = (
        r'<!--\s*Specifications Table\s*-->\s*<div\b[^>]*>.*?</div>\s*(?=<div\b|$)',
        r'<div\b[^>]*>\s*<h3\b[^>]*>\s*SPECIFICATIONS\s*</h3>.*?</div>\s*(?=<div\b|$)',
    )
    for pattern in patterns:
        updated, count = re.subn(
            pattern,
            rebuilt_table_html,
            original_html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if count:
            return updated

    return original_html.rstrip() + "\n" + rebuilt_table_html


def _description_has_substantive_copy(description: str) -> bool:
    text = strip_html_text(description)
    if not text:
        return False
    text = re.sub(
        r"\b(?:specifications|package includes|assembly required|overall dimensions|weight|material)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return len(re.findall(r"[A-Za-z0-9]+", text)) >= 8


def description_has_key_features_template(description: str) -> bool:
    if not description:
        return False
    lowered = description.lower()
    return "key features" in lowered and "<li" in lowered


# Raw GIGA source field labels (Chinese). Their presence means the listing is
# showing the untemplated supplier data dump, NOT the store template — even
# when an English "KEY FEATURES" string happens to also be in the raw source.
_RAW_SOURCE_MARKERS = ("产品规格", "基础信息", "产品名称:", "产品类型:", "产品尺寸", "组装长度")


def description_is_raw_source_dump(description: str) -> bool:
    if not description:
        return False
    return sum(1 for m in _RAW_SOURCE_MARKERS if m in description) >= 2


def description_has_visible_template_artifacts(description: str) -> bool:
    if not description:
        return False
    if re.search(r"\\[nrt]", description):
        return True
    return bool(
        re.search(
            r'>\s*(?:(?:&quot;|&#34;|")\s*>|\s*(?:&quot;|&#34;|"))\s*(?=<)',
            description,
            flags=re.IGNORECASE,
        )
    )


def extract_source_feature_bullets(source_description: str, *, limit: int = 6) -> list[str]:
    bullets = []
    seen = set()

    for raw_item in re.findall(r"<li\b[^>]*>(.*?)</li>", source_description or "", flags=re.IGNORECASE | re.DOTALL):
        text = strip_html_text(raw_item)
        text = re.sub(r"\s+", " ", text).strip(" -•")
        if len(re.findall(r"[A-Za-z0-9]+", text)) < 5:
            continue
        normalized = text.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        bullets.append(text)
        if len(bullets) >= limit:
            return bullets

    source_text = strip_html_text(source_description or "")
    for sentence in re.split(r"(?<=[.!?])\s+", source_text):
        text = re.sub(r"\s+", " ", sentence).strip(" -•")
        # Section headings glue onto the first sentence after HTML stripping.
        text = re.sub(r"^(?:Product Features|Features|Specifications)\s+", "", text, flags=re.IGNORECASE)
        if len(re.findall(r"[A-Za-z0-9]+", text)) < 6:
            continue
        normalized = text.casefold()
        if normalized in seen:
            continue
        # A sentence carved out of an already-captured bullet is a fragment,
        # not a new selling point — padding with it just duplicates copy.
        if any(normalized in existing or existing in normalized for existing in seen):
            continue
        seen.add(normalized)
        bullets.append(text)
        if len(bullets) >= limit:
            break

    return bullets


def _build_perfect_for_copy(title: str, source_description: str, aspects: dict) -> str:
    source_text = strip_html_text(source_description or "")
    for sentence in re.split(r"(?<=[.!?])\s+", source_text):
        cleaned = re.sub(r"\s+", " ", sentence).strip()
        if any(
            marker in cleaned.lower()
            for marker in ("ideal", "living room", "bedroom", "office", "reading", "application", "interior spaces")
        ):
            return cleaned

    room = first_aspect_text(aspects, "Room")
    type_name = first_aspect_text(aspects, "Type") or title or "furniture piece"
    room_text = room.lower() if room else "everyday indoor spaces"
    return f"Ideal for {room_text} where a compact {type_name.lower()} adds practical seating and everyday comfort."


def _build_package_includes_copy(title: str, source_description: str, aspects: dict, assembly_required: str) -> str:
    package_items = []
    type_name = first_aspect_text(aspects, "Type") or title or "Main Product"
    package_items.append(f"1 x {type_name}")

    source_text = f"{title} {source_description}".lower()
    if "lumbar pillow" in source_text:
        package_items.append("1 x Detachable Lumbar Pillow")

    if assembly_required == "Yes":
        package_items.append("1 x Hardware Kit")
        package_items.append("1 x Assembly Instructions")

    deduped = []
    seen = set()
    for item in package_items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return ", ".join(deduped)


def parse_image_list(val):
    if isinstance(val, list):
        return [item for item in val if isinstance(item, str) and item.strip()]
    if isinstance(val, str) and val.strip():
        try:
            parsed = json.loads(val)
        except Exception:
            parsed = []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, str) and item.strip()]
    return []


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return set()

    columns = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            columns.add(str(row["name"]))
        elif len(row) >= 2:
            columns.add(str(row[1]))
    return columns


def build_source_audit_payload(
    title: str,
    description: str,
    attributes_raw,
    specs_raw,
    *,
    images_raw=None,
    videos_raw=None,
) -> dict:
    return {
        "title": title or "",
        "description": description or "",
        "attributes": parse_json(attributes_raw),
        "specs": parse_json(specs_raw),
        "images": parse_image_list(images_raw),
        "videos": parse_image_list(videos_raw),
    }


def build_live_audit_payload(opt_raw, *, listing_id=None, live_inventory=None) -> dict:
    opt = parse_json(opt_raw)
    product = (live_inventory or {}).get("product") or {}
    raw_description = opt.get("description", "") or ""
    return {
        "listing_id": str(listing_id or ""),
        "title": opt.get("title", "") or "",
        # Keep raw HTML in the fingerprint so visible artifact regressions
        # cannot be masked by sanitizer-based normalization.
        "description": raw_description,
        "description_sanitized": sanitize_generated_description_html(raw_description),
        "categoryId": str(opt.get("categoryId", "") or ""),
        "aspects": opt.get("aspects", {}) if isinstance(opt.get("aspects"), dict) else {},
        "imageUrls": parse_image_list(product.get("imageUrls") or []),
        "videoIds": parse_image_list(product.get("videoIds") or []),
    }


def build_audit_fingerprint(payload: dict) -> str:
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def get_quality_gate_meta(opt: dict) -> dict:
    if not isinstance(opt, dict):
        return {}
    meta = opt.get(QUALITY_GATE_META_KEY)
    return meta if isinstance(meta, dict) else {}


def split_transport_issues(issues: list[dict]) -> tuple[list[dict], list[dict]]:
    transport_issues = []
    content_issues = []
    for issue in issues or []:
        issue_type = str((issue or {}).get("type", "")).strip()
        if issue_type in TRANSPORT_ISSUE_TYPES:
            transport_issues.append(issue)
        else:
            content_issues.append(issue)
    return transport_issues, content_issues


def is_listing_frozen_clean(
    opt: dict,
    *,
    source_fingerprint: str,
    live_fingerprint: str,
    listing_id: str | None,
) -> bool:
    meta = get_quality_gate_meta(opt)
    if meta.get("version") != QUALITY_GATE_META_VERSION:
        return False
    if meta.get("ruleset_version") != QUALITY_GATE_RULESET_VERSION:
        return False
    if meta.get("status") != "clean":
        return False
    return (
        meta.get("source_fingerprint") == source_fingerprint
        and meta.get("live_fingerprint") == live_fingerprint
        and str(meta.get("listing_id", "") or "") == str(listing_id or "")
    )


def persist_listing_audit_state(
    db_conn: sqlite3.Connection,
    sku: str,
    *,
    state: str,
    source_fingerprint: str | None = None,
    live_fingerprint: str | None = None,
    listing_id: str | None = None,
    issue_types: list[str] | None = None,
    touch_updated_at: bool = False,
) -> bool:
    row = db_conn.execute(
        "SELECT optimization FROM collected_products WHERE sku = ?",
        (sku,),
    ).fetchone()
    if not row:
        return False

    opt = parse_json(row["optimization"] if isinstance(row, sqlite3.Row) else row[0])
    if not isinstance(opt, dict):
        opt = {}

    current_meta = get_quality_gate_meta(opt)
    normalized_listing_id = str(listing_id or "")
    normalized_issue_types = sorted({str(issue) for issue in (issue_types or []) if str(issue).strip()})

    if state == "clean":
        if (
            current_meta.get("version") == QUALITY_GATE_META_VERSION
            and current_meta.get("ruleset_version") == QUALITY_GATE_RULESET_VERSION
            and current_meta.get("status") == "clean"
            and current_meta.get("source_fingerprint") == source_fingerprint
            and current_meta.get("live_fingerprint") == live_fingerprint
            and str(current_meta.get("listing_id", "") or "") == normalized_listing_id
        ):
            return False
    elif state in {"dirty", "pending_verify"}:
        if (
            current_meta.get("version") == QUALITY_GATE_META_VERSION
            and current_meta.get("ruleset_version") == QUALITY_GATE_RULESET_VERSION
            and current_meta.get("status") == state
            and current_meta.get("source_fingerprint") == source_fingerprint
            and current_meta.get("live_fingerprint") == live_fingerprint
            and str(current_meta.get("listing_id", "") or "") == normalized_listing_id
            and sorted(current_meta.get("issue_types") or []) == normalized_issue_types
        ):
            return False

    now_iso = datetime.now().isoformat()
    meta = {
        "version": QUALITY_GATE_META_VERSION,
        "ruleset_version": QUALITY_GATE_RULESET_VERSION,
        "status": state,
        "updated_at": now_iso,
        "listing_id": normalized_listing_id,
    }
    if source_fingerprint:
        meta["source_fingerprint"] = source_fingerprint
    if live_fingerprint:
        meta["live_fingerprint"] = live_fingerprint
    if normalized_issue_types:
        meta["issue_types"] = normalized_issue_types
    if state == "clean":
        meta["verified_clean_at"] = now_iso
    elif state == "pending_verify":
        meta["fix_applied_at"] = now_iso
    elif state == "dirty":
        meta["observed_dirty_at"] = now_iso

    opt[QUALITY_GATE_META_KEY] = meta
    columns = _table_columns(db_conn, "collected_products")
    assignments = ["optimization = ?"]
    params = [json.dumps(opt, ensure_ascii=False)]
    if touch_updated_at and "updated_at" in columns:
        assignments.append("updated_at = ?")
        params.append(datetime.now().isoformat(sep=" ", timespec="seconds"))
    params.append(sku)
    db_conn.execute(
        f"UPDATE collected_products SET {', '.join(assignments)} WHERE sku = ?",
        params,
    )
    db_conn.commit()
    return True


def build_claim_cleanup_pattern(claim_text: str) -> str:
    normalized_claim = str(claim_text or "").strip()

    claimed_match = re.search(
        r"claimed\s+(.+?)(?:,\s*source\s+has\s+.+)?$",
        normalized_claim,
        flags=re.IGNORECASE,
    )
    if claimed_match:
        normalized_claim = claimed_match.group(1).strip()

    feature_patterns = FEATURE_CLAIM_PATTERNS.get(claim_text)
    if feature_patterns:
        return "(?:" + "|".join(feature_patterns) + ")"

    wood_patterns = WOOD_SPECIES_PATTERNS.get(claim_text)
    if wood_patterns:
        return "(?:" + "|".join(wood_patterns) + ")"

    qty_match = re.fullmatch(r"(\d+)[\s\-_]+([a-z]+)", normalized_claim, flags=re.IGNORECASE)
    if qty_match:
        count, claim_name = qty_match.groups()
        claim_pattern = COUNTABLE_CLAIM_FIX_PATTERNS.get(claim_name.lower())
        if claim_pattern and claim_name.lower() in COUNTABLE_CLAIMS:
            return rf"\b{re.escape(count)}[\s\-_]*{claim_pattern}\b"

    if "_" in claim_text:
        parts = [re.escape(part) for part in claim_text.split("_") if part]
        if parts:
            separator = r"[\s\-_]+"
            return r"\b(?:" + separator.join(parts) + r")\b"

    parts = [re.escape(part) for part in re.split(r"[\s\-_]+", normalized_claim) if part]
    if parts:
        return r"\b(?:" + r"[\s\-_]+".join(parts) + r")\b"

    return rf"\b{re.escape(normalized_claim)}\b"


def extract_num(text):
    m = re.search(r'(\d+\.?\d*)', text or "")
    return float(m.group(1)) if m else None


def strip_html_text(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_source_material_hint(attrs, description: str) -> str:
    for key in ("Material", "Frame Material", "Main Material", "材质"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()

    text = strip_html_text(description)
    patterns = (
        r"(?:材质|Material)\s*[:：]\s*([A-Za-z0-9+/\-& ,]+)",
        r"(?:Made of|Constructed of)\s+([A-Za-z0-9+/\-& ,]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            material = re.sub(r"\s+", " ", match.group(1)).strip(" -,:;.")
            if material:
                return material
    return ""


def _find_matching_labels(text: str, pattern_map: dict[str, tuple[str, ...]]) -> list[str]:
    matches = []
    for label, patterns in pattern_map.items():
        if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
            matches.append(label)
    return matches


def _remove_html_blocks_containing(description: str, patterns: tuple[str, ...]) -> str:
    if not description:
        return description

    combined = "(?:" + "|".join(patterns) + ")"
    for tag in ("li", "p", "tr"):
        description = re.sub(
            rf"<{tag}\b[^>]*>.*?{combined}.*?</{tag}>",
            "",
            description,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return description


def extract_validated_numeric_aspect(aspects, key, *, max_value):
    text = first_aspect_text(aspects, key)
    issue = measurement_issue(text, max_value=max_value)
    if issue:
        return text, issue, None

    numeric_value = extract_num(text)
    if numeric_value is None:
        return text, "missing", None

    return text, None, numeric_value


def audit_single_product(
    sku,
    title,
    attrs_raw,
    specs_raw,
    opt_raw,
    description,
    ebay_client=None,
    images_raw=None,
    videos_raw=None,
    live_inventory=None,
):
    """Audit a single product. Returns (issues, fixes) where fixes are actionable corrections."""
    issues = []
    fixes = {}  # {field: new_value} for aspects that need fixing

    attrs = parse_json(attrs_raw)
    specs = parse_json(specs_raw)
    opt = parse_json(opt_raw)
    aspects = opt.get("aspects", {}) if isinstance(opt, dict) else {}
    stored_category = str(opt.get("categoryId", "") or "").strip()
    stored_category_name = opt.get("categoryName") if isinstance(opt, dict) else None
    raw_live_description = opt.get("description", description or "") if isinstance(opt, dict) else description or ""
    live_description = sanitize_generated_description_html(raw_live_description)
    title = title or ""
    title_lower = title.lower()

    # Also check the optimized title
    opt_title = opt.get("title", "") if isinstance(opt, dict) else ""
    combined_title = f"{title} {opt_title}".lower()
    matcher = get_category_matcher()
    title_context = " ".join(part for part in (title, opt_title) if part).strip()
    db_images = parse_image_list(images_raw)
    db_videos = parse_image_list(videos_raw)
    cleaned_title, title_changed = sanitize_listing_title(opt_title or title or "")

    if title_changed and cleaned_title:
        issues.append({
            "type": "title_cleanup",
            "severity": "HIGH",
            "current": opt_title or title,
            "expected": cleaned_title,
            "detail": "Title contains non-product video/assembly marker text and should be cleaned",
        })
        fixes["__title__"] = cleaned_title

    if ebay_client and (len(db_images) >= 2 or bool(db_videos) or videos_raw is not None):
        try:
            if live_inventory is None:
                live_inventory = ebay_client.get_inventory_item(sku) or {}
            live_product = (live_inventory.get("product") or {})
            live_image_urls = parse_image_list(live_product.get("imageUrls") or [])
            if len(db_images) >= 2:
                expected_image_count = min(len(db_images), 24)
                live_image_count = len(live_image_urls)
                if live_image_count < 2:
                    issues.append({
                        "type": "image_collapse",
                        "severity": "CRITICAL",
                        "current": live_image_count,
                        "expected": expected_image_count,
                        "detail": (
                            f"Live inventory imageUrls collapsed to {live_image_count} while local DB has "
                            f"{expected_image_count} source images"
                        ),
                    })
                    fixes["__restore_images__"] = True

            live_video_ids = parse_image_list(live_product.get("videoIds") or [])
            if db_videos and not live_video_ids:
                issues.append({
                    "type": "missing_video",
                    "severity": "HIGH",
                    "current": 0,
                    "expected": len(db_videos),
                    "detail": (
                        f"GIGA source has {len(db_videos)} video(s) but live eBay inventory has no videoIds"
                    ),
                })
                fixes["__sync_video__"] = db_videos[0]
            elif not db_videos and live_video_ids:
                issues.append({
                    "type": "stale_video",
                    "severity": "HIGH",
                    "current": len(live_video_ids),
                    "expected": 0,
                    "detail": "Live eBay inventory still has videoIds but current GIGA/source data has no video",
                })
                fixes["__remove_video__"] = True
        except Exception as exc:
            logging.warning(f"[AUDIT] {sku}: failed to inspect live listing media: {exc}")

    if description_has_visible_template_artifacts(raw_live_description):
        issues.append({
            "type": "description_html_artifacts",
            "severity": "HIGH",
            "field": "description",
            "expected": "clean HTML without visible escape sequences or orphan quote nodes",
            "detail": "Live description contains visible HTML artifacts such as literal \\n or orphan quote markers between tags",
        })
        fixes["__sanitize_live_description_html__"] = True

    # Raw-source dump guard — checked FIRST because such a description also
    # contains an English "KEY FEATURES" string and would otherwise pass the
    # template check below (this gap silently shipped ~79 untemplated live
    # descriptions; found 2026-07-14 via W6018P506376).
    if description_is_raw_source_dump(raw_live_description):
        issues.append({
            "type": "description_raw_source_dump",
            "severity": "CRITICAL",
            "field": "description",
            "expected": "store AQUAVERVE template rebuilt from source characteristics",
            "detail": "Live description is the raw GIGA supplier data dump (Chinese field labels), not the store template",
        })
        fixes["__rebuild_description_from_source__"] = True
    elif live_description and not description_has_key_features_template(live_description):
        issues.append({
            "type": "description_structure_missing_key_features",
            "severity": "HIGH",
            "field": "description",
            "expected": "KEY FEATURES heading with bullet list",
            "detail": "Live description is missing the KEY FEATURES section heading and/or bullet list structure",
        })
        fixes["__restore_live_description_from_local__"] = True

    # ── 1. Category Check — deterministic product profile wins over taxonomy drift ──
    profile = classify_listing_profile(title_context or title, live_description, stored_category)
    if profile.category_id:
        if stored_category != profile.category_id:
            issues.append({
                "type": "category_mismatch",
                "severity": "CRITICAL",
                "current": stored_category or None,
                "expected": profile.category_id,
                "detail": (
                    f"Product profile '{profile.kind}' should use category {profile.category_id} "
                    f"for '{opt_title[:60] or title[:60]}'"
                ),
            })
            fixes["categoryId"] = profile.category_id
            if profile.category_name:
                fixes["categoryName"] = profile.category_name
    elif stored_category:
        canonical_category, canonical_name = matcher.canonicalize_category(
            title_context,
            stored_category,
            stored_category_name,
            live_description,
        )

        if canonical_category and canonical_category != stored_category:
            issues.append({
                "type": "category_mismatch",
                "severity": "CRITICAL",
                "current": stored_category,
                "expected": canonical_category,
                "detail": f"Stale category {stored_category} should be {canonical_category} for '{opt_title[:60] or title[:60]}'"
            })
            fixes["categoryId"] = canonical_category
            if canonical_name:
                fixes["categoryName"] = canonical_name
        elif not matcher.is_category_plausible_for_text(combined_title, stored_category, stored_category_name):
            suggested_category, suggested_name, _ = matcher.get_category_and_aspects(
                title_context,
                aspects,
                live_description,
            )
            suggested_category, suggested_name = matcher.canonicalize_category(
                title_context,
                suggested_category,
                suggested_name,
                live_description,
            )
            if suggested_category and suggested_category != stored_category:
                issues.append({
                    "type": "category_mismatch",
                    "severity": "CRITICAL",
                    "current": stored_category,
                    "expected": suggested_category,
                    "detail": f"Implausible category {stored_category} for '{opt_title[:60] or title[:60]}'; expected {suggested_category}"
                })
                fixes["categoryId"] = suggested_category
                if suggested_name:
                    fixes["categoryName"] = suggested_name

    # ── 2. Dimension Accuracy ──
    source_dims = extract_all_dimensions(attrs)

    # Compressed-shipping trap: some suppliers copy the box dimensions into
    # the assembled fields (assembled == package on every axis). When the
    # title itself declares a much larger size, the assembled values are
    # untrustworthy — "fixing" live dims to them would shrink a 71" sofa to
    # its shipping box (live incident W5571P440912, 2026-07-13).
    source_dims_trustworthy = True
    package_axes = [
        extract_num(specs.get(key)) if specs.get(key) else None
        for key in ("Package Length (in.)", "Package Width (in.)", "Package Height (in.)")
    ]
    assembled_axes = [source_dims.get("length"), source_dims.get("width"), source_dims.get("height")]
    if all(v is not None for v in package_axes) and all(v is not None for v in assembled_axes):
        axes_match_package = all(
            abs(assembled - package) <= 0.15
            for assembled, package in zip(assembled_axes, package_axes)
        )
        title_inches = [
            float(m)
            for m in re.findall(r'(\d{2,3}(?:\.\d+)?)\s*(?:"|inch|in\b)', f"{title} {opt_title}".lower())
        ]
        if axes_match_package and title_inches and max(title_inches) > max(assembled_axes) + 5:
            source_dims_trustworthy = False
            issues.append({
                "type": "suspect_source_dimensions",
                "severity": "HIGH",
                "detail": (
                    f"Source assembled dims {assembled_axes} equal package dims but title claims "
                    f"{max(title_inches):g}\" — supplier likely filled box dims; dimension auto-fix suppressed"
                ),
            })

    dim_checks = [
        ("Item Length", source_dims.get("length")),
        ("Item Width", source_dims.get("width")),
        ("Item Height", source_dims.get("height")),
    ]

    for aspect_key, source_val in dim_checks:
        if source_val is None or not source_dims_trustworthy:
            continue

        current_val_str, current_issue, current_num = extract_validated_numeric_aspect(
            aspects,
            aspect_key,
            max_value=500,
        )

        if current_issue:
            issues.append({
                "type": "missing_dimension" if current_issue in {"missing", "placeholder"} else "wrong_dimension",
                "severity": "MEDIUM" if current_issue in {"missing", "placeholder"} else "CRITICAL",
                "field": aspect_key,
                "current": current_val_str or None,
                "source_value": source_val,
                "detail": f"{aspect_key} has {current_issue} value '{current_val_str or 'empty'}', source has {source_val} in"
            })
            fixes[aspect_key] = [f"{source_val} in"]
        elif abs(current_num - source_val) > 1.0:
            issues.append({
                "type": "wrong_dimension",
                "severity": "CRITICAL",
                "field": aspect_key,
                "current": current_num,
                "expected": source_val,
                "detail": f"{aspect_key}: listing={current_num} vs source={source_val} (diff: {abs(current_num - source_val):.1f})"
            })
            fixes[aspect_key] = [f"{source_val} in"]

    # Check weight
    source_weight = source_dims.get("weight")
    desc_weight = extract_product_weight_from_text(description)
    package_weight = extract_num(specs.get("Package Weight (lbs.)")) if specs.get("Package Weight (lbs.)") else None
    if source_weight is None:
        source_weight = desc_weight
    elif package_weight is not None and abs(source_weight - package_weight) < 0.01:
        if desc_weight is not None and abs(desc_weight - package_weight) > 0.5:
            source_weight = desc_weight
        else:
            source_weight = None
    if source_weight and source_dims_trustworthy:
        weight_key = next((wk for wk in ["Item Weight", "Product Weight"] if wk in aspects), "Item Weight")
        weight_val_str, weight_issue, weight_num = extract_validated_numeric_aspect(
            aspects,
            weight_key,
            max_value=2000,
        )
        if weight_issue:
            issues.append({
                "type": "missing_weight" if weight_issue in {"missing", "placeholder"} else "wrong_weight",
                "severity": "MEDIUM" if weight_issue in {"missing", "placeholder"} else "HIGH",
                "field": weight_key,
                "current": weight_val_str or None,
                "source_value": source_weight,
                "detail": f"{weight_key} has {weight_issue} value '{weight_val_str or 'empty'}', source has {source_weight} lbs"
            })
            fixes["Item Weight"] = [f"{source_weight} lbs"]
        elif abs(weight_num - source_weight) > 5.0:
            issues.append({
                "type": "wrong_weight",
                "severity": "HIGH",
                "current": weight_num,
                "expected": source_weight,
                "detail": f"Item Weight: listing={weight_num} vs source={source_weight}"
            })
            fixes["Item Weight"] = [f"{source_weight} lbs"]

    # ── 3. Non-applicable Aspects ──
    for aspect_key, should_remove_fn in NON_APPLICABLE_RULES.items():
        if aspect_key in aspects and should_remove_fn(combined_title):
            issues.append({
                "type": "non_applicable_aspect",
                "severity": "MEDIUM",
                "field": aspect_key,
                "value": aspects[aspect_key],
                "detail": f"'{aspect_key}' not applicable for this product type"
            })
            fixes[f"__remove__{aspect_key}"] = True

    # ── 3.5 Source-protected assembly requirement ──
    source_assembly = infer_source_assembly_required(attrs, specs, description)
    current_assembly = first_aspect_text(aspects, "Assembly Required")
    current_assembly_status = first_aspect_text(aspects, "Assembly Status")
    source_assembly_status = infer_source_assembly_status(attrs, specs, description)
    if current_assembly_status and not source_assembly_status:
        issues.append({
            "type": "assembly_status_unsupported",
            "severity": "HIGH",
            "field": "Assembly Status",
            "current": current_assembly_status,
            "expected": "remove",
            "detail": "Assembly Status has no supplier-source evidence and should be removed"
        })
        fixes["__remove__Assembly Status"] = True

    if source_assembly:
        if current_assembly.lower() != source_assembly.lower():
            issues.append({
                "type": "assembly_required_mismatch",
                "severity": "CRITICAL",
                "field": "Assembly Required",
                "current": current_assembly or None,
                "expected": source_assembly,
                "detail": f"Assembly Required must be {source_assembly} because supplier source says Assembly Required {source_assembly}"
            })
            fixes["Assembly Required"] = [source_assembly]

        contradictions = find_assembly_description_contradictions(live_description, source_assembly)
        if contradictions:
            issues.append({
                "type": "assembly_description_contradiction",
                "severity": "CRITICAL",
                "field": "description",
                "expected": f"Assembly Required {source_assembly}",
                "detail": f"Description contains wording that contradicts supplier Assembly Required {source_assembly}"
            })
            fixes["__assembly_desc_update__"] = source_assembly
        packaging = aspects.get("Packaging")
        packaging_values = packaging if isinstance(packaging, list) else [packaging] if packaging else []
        if source_assembly == "Yes" and any("fully assembled" in str(value).lower() for value in packaging_values):
            issues.append({
                "type": "assembly_packaging_contradiction",
                "severity": "CRITICAL",
                "field": "Packaging",
                "current": packaging_values,
                "expected": "remove",
                "detail": "Packaging says Fully Assembled while supplier source says assembly is required"
            })
            fixes["__remove__Packaging"] = True

    effective_assembly = source_assembly or (current_assembly if current_assembly in {"Yes", "No"} else None)
    if effective_assembly == "Yes" and not has_expected_assembly_copy(live_description, effective_assembly):
        issues.append({
            "type": "assembly_description_missing",
            "severity": "HIGH",
            "field": "description",
            "expected": "explicit assembly-required wording",
            "detail": "Description should explicitly say assembly is required when item specifics say Assembly Required=Yes"
        })
        fixes["__assembly_desc_update__"] = effective_assembly

    # ── 4. AI-hallucinated Product Dimensions ──
    if "Product Dimensions" in aspects and source_dims.get("length") and source_dims_trustworthy:
        pd_str = aspects["Product Dimensions"][0] if isinstance(aspects["Product Dimensions"], list) else str(aspects["Product Dimensions"])
        # Check if the Product Dimensions string matches source
        nums_in_pd = re.findall(r'(\d+\.?\d*)', pd_str)
        source_vals = [source_dims["length"], source_dims.get("width"), source_dims.get("height")]
        source_vals = [v for v in source_vals if v is not None]
        if nums_in_pd and source_vals:
            pd_nums = [float(n) for n in nums_in_pd[:3]]
            # Check if any PD number is far from all source values
            mismatch_count = 0
            for pd_n in pd_nums:
                if not any(abs(pd_n - sv) < 2.0 for sv in source_vals):
                    mismatch_count += 1
            if mismatch_count >= 2:
                correct_pd = f'{source_dims["length"]}"L x {source_dims.get("width", "?")}"W x {source_dims.get("height", "?")}"H'
                issues.append({
                    "type": "wrong_product_dimensions",
                    "severity": "CRITICAL",
                    "current": pd_str,
                    "expected": correct_pd,
                    "detail": f"Product Dimensions string doesn't match source: '{pd_str}' vs {correct_pd}"
                })
                fixes["Product Dimensions"] = [correct_pd]

    # ── 5. Description dimension consistency ──
    if live_description and source_dims_trustworthy and source_dims.get("length") and source_dims.get("width") and source_dims.get("height"):
        desc_clean = re.sub(r'<[^>]+>', ' ', live_description).lower()
        l_str = str(source_dims["length"])
        w_str = str(source_dims["width"])
        h_str = str(source_dims["height"])
        found = sum(1 for d in [l_str, w_str, h_str] if d in desc_clean)
        if found < 2:
            wrong_dims_in_desc = []
            # Try to find what dimensions ARE in the description
            dim_pattern = r'(\d+\.?\d*)\s*["\']?\s*[×xX]\s*(\d+\.?\d*)\s*["\']?\s*[×xX]\s*(\d+\.?\d*)'
            m = re.search(dim_pattern, desc_clean)
            if m:
                desc_l, desc_w, desc_h = float(m.group(1)), float(m.group(2)), float(m.group(3))
                if abs(desc_l - source_dims["length"]) > 2 or abs(desc_w - source_dims["width"]) > 2 or abs(desc_h - source_dims["height"]) > 2:
                    wrong_dims_in_desc = [desc_l, desc_w, desc_h]

            issues.append({
                "type": "desc_dimension_mismatch",
                "severity": "HIGH",
                "expected": f"{l_str}×{w_str}×{h_str}",
                "found_in_desc": wrong_dims_in_desc if wrong_dims_in_desc else "dimensions not found",
                "detail": f"Description has wrong/missing dimensions (source: {l_str}×{w_str}×{h_str})"
            })
            fixes["__desc_needs_update__"] = True

    if live_description and source_weight and source_dims_trustworthy:
        desc_clean = re.sub(r'<[^>]+>', ' ', live_description).lower()
        weight_variants = {
            str(source_weight),
            f"{float(source_weight):g}",
            f"{float(source_weight):.1f}".rstrip("0").rstrip("."),
            f"{float(source_weight):.2f}".rstrip("0").rstrip("."),
        }
        if not any(variant and variant in desc_clean for variant in weight_variants):
            issues.append({
                "type": "desc_weight_mismatch",
                "severity": "HIGH",
                "expected": f"{source_weight} lbs",
                "detail": f"Description has wrong/missing item weight (source: {source_weight} lbs)"
            })
            fixes["__desc_needs_update__"] = True

    # ── 6. Motors Compatibility ──
    compat_title = opt_title or title
    compat_desc = live_description
    if stored_category in EBAY_MOTORS_CATEGORIES:
        compatibility = analyze_ebay_motors_compatibility(stored_category, compat_title, compat_desc, aspects)
        expected_aspects = apply_compatibility_aspects(stored_category, aspects, compatibility)
        current_meta = opt.get("motorsCompatibility", {}) if isinstance(opt, dict) else {}

        if compatibility.mode == "specific" and not current_meta.get("compatibleProducts"):
            issues.append({
                "type": "missing_motors_compatibility",
                "severity": "HIGH",
                "detail": f"Structured Motors compatibility missing; expected {len(compatibility.compatible_products)} fitment rows"
            })
            fixes["__motors_compatibility__"] = serialize_compatibility_analysis(compatibility)
        elif compatibility.mode != "specific" and current_meta.get("compatibleProducts"):
            issues.append({
                "type": "stale_motors_compatibility",
                "severity": "HIGH",
                "detail": f"Stored structured compatibility should be cleared (mode={compatibility.mode})"
            })
            fixes["__motors_compatibility__"] = serialize_compatibility_analysis(compatibility)
        elif current_meta.get("mode") != compatibility.mode:
            issues.append({
                "type": "motors_compatibility_metadata",
                "severity": "MEDIUM",
                "detail": f"Motors compatibility metadata stale: stored={current_meta.get('mode', 'none')} expected={compatibility.mode}"
            })
            fixes["__motors_compatibility__"] = serialize_compatibility_analysis(compatibility)

        for aspect_key, expected_val in expected_aspects.items():
            if aspects.get(aspect_key) != expected_val:
                issues.append({
                    "type": "motors_aspect_fix",
                    "severity": "MEDIUM",
                    "field": aspect_key,
                    "detail": f"Motors aspect '{aspect_key}' needs normalization"
                })
                fixes[aspect_key] = expected_val

        for removable_key in ("Compatible Year", "Compatible Make", "Compatible Model", "Model"):
            if removable_key in aspects and removable_key not in expected_aspects:
                issues.append({
                    "type": "motors_aspect_remove",
                    "severity": "MEDIUM",
                    "field": removable_key,
                    "detail": f"Motors aspect '{removable_key}' should be removed"
                })
                fixes[f"__remove__{removable_key}"] = True

    # ── 7. Comprehensive Hallucination Check ──
    opt_title_l = (opt_title or "").lower()
    opt_desc_l = (live_description or "").lower()
    giga_title_l = title.lower()
    giga_desc_l = (description or "").lower()

    # ── Layer 2: Claim Diff Engine ──
    try:
        _source_constraints = build_source_constraints(
            source_title=title,
            source_description=description or "",
            attrs=attrs,
            specs=specs,
        )
        _claim_violations = detect_claim_violations(
            source_constraints=_source_constraints,
            generated_title=opt_title,
            generated_description=live_description,
            generated_aspects=aspects,
        )
        _existing_types = {i["type"] for i in issues}
        _claim_violation_texts = []
        for _cv in _claim_violations:
            if _cv.severity in ("CRITICAL", "HIGH"):
                _claim_violation_texts.append(_cv.claim_text)
            _issue_type = f"claim_{_cv.claim_type}"
            if _issue_type not in _existing_types:
                issues.append({
                    "type": _issue_type,
                    "severity": _cv.severity,
                    "detail": f"[ClaimDiffEngine] {_cv.claim_text} in {_cv.location}: source={_cv.source_evidence}",
                })
        if _claim_violation_texts:
            fixes["__claim_diff_violations__"] = _claim_violation_texts
    except Exception as e:
        print(f"ClaimDiffEngine error for sku {sku}: {e}")

    # ── Layer 2.5: Semantic fact-sheet guard (LLM extract → deterministic diff) ──
    # Report-only net for the long tail the rule tables cannot enumerate
    # (e.g. source "600D Oxford" published as "Canvas" — no upgrade chain
    # existed, so Layer 2 stayed silent). Extraction results are cached by
    # content hash, so the LLM only runs when source or live content changed.
    if os.getenv("AUDIT_SEMANTIC_FACT_SHEET", "1") != "0" and os.getenv("QWEN_API_KEY"):
        try:
            from src.utils.listing_fact_sheet import compare_fact_sheets, fact_sheet_for_content

            _fs_conn = get_fact_sheet_conn()
            _source_sheet = fact_sheet_for_content(_fs_conn, title, description or "", {**attrs, **specs})
            _live_sheet = fact_sheet_for_content(_fs_conn, opt_title, live_description, aspects)
            if _source_sheet and _live_sheet:
                _existing_details = " || ".join(str(i.get("detail", "")) for i in issues).lower()
                _strip_fs = lambda h: re.sub(r"<[^>]+>", " ", str(h or ""))
                _live_txt = " ".join([opt_title or "", _strip_fs(live_description),
                                      " ".join(f"{k}: {v}" for k, v in (aspects or {}).items())])
                _source_txt = " ".join([title or "", _strip_fs(description),
                                       " ".join(f"{k}: {v}" for k, v in {**attrs, **specs}.items())])
                for _sv in compare_fact_sheets(_source_sheet, _live_sheet, live_text=_live_txt, source_text=_source_txt):
                    if _sv["claim_type"] == "semantic_dimension" and not source_dims_trustworthy:
                        continue  # source dims are box dims — comparison is meaningless
                    if _sv["claim_text"].lower() in _existing_details:
                        continue  # already reported by a rule layer
                    issues.append({
                        "type": _sv["claim_type"],
                        "severity": _sv["severity"],
                        "detail": f"[FactSheet] {_sv['claim_text']} — source evidence: {_sv['source_evidence']}",
                    })
        except Exception as e:
            print(f"FactSheet guard error for sku {sku}: {e}")
    
    giga_text = f"{giga_title_l} {giga_desc_l} " + " ".join(str(v).lower() for v in attrs.values())
    opt_text = f"{opt_title_l} {opt_desc_l} " + " ".join(" ".join(str(x).lower() for x in v) if isinstance(v, list) else str(v).lower() for v in aspects.values())

    # 1. TSA Lock Hallucination
    tsa_patterns = [r'\btsa\b', r'\btransportation\s+security\s+administration\b']
    opt_has_tsa = any(re.search(pat, opt_text) for pat in tsa_patterns)
    giga_has_tsa = any(re.search(pat, giga_text) for pat in tsa_patterns)
    if opt_has_tsa and not giga_has_tsa:
        issues.append({
            "type": "hallucinated_tsa_lock",
            "severity": "CRITICAL",
            "detail": "TSA lock is referenced in optimization but the supplier source only specifies a normal combination lock"
        })
        fixes["__hallucinated_tsa__"] = True

    # 2. Charging Station Hallucination
    charging_patterns = [r'\busb\b', r'\bpower\s+outlet\b', r'\bcharging\s+station\b', r'\bpower\s+strip\b', r'\bbuilt-in\s+outlet\b', r'\bcharger\b']
    opt_has_charging = any(re.search(pat, opt_text) for pat in charging_patterns)
    giga_has_charging = any(re.search(pat, giga_text) for pat in [r'\busb\b', r'\boutlet\b', r'\bcharging\b', r'\bpower\s+strip\b', r'插座', r'充电', r'\bcharger\b'])
    if opt_has_charging and not giga_has_charging:
        issues.append({
            "type": "hallucinated_charging",
            "severity": "CRITICAL",
            "detail": "USB charging ports or power outlets are optimized but GIGA source doesn't mention any power features"
        })
        fixes["__hallucinated_charging__"] = True

    # 3. Genuine Leather Hallucination
    opt_has_leather = bool(re.search(r'\bgenuine\s+leather\b', opt_text)) or any("leather" in str(x).lower() and "faux" not in str(x).lower() and "pu" not in str(x).lower() for x in aspects.get("Material", []))
    giga_has_leather = bool(re.search(r'\b(?:genuine|real)\s+leather\b', giga_text))
    giga_is_faux = any(re.search(pat, giga_text) for pat in [r'\bpu\s+leather\b', r'\bfaux\s+leather\b', r'\bpu\b', r'\bpvc\b', r'\bsynthetic\s+leather\b'])
    if opt_has_leather and giga_is_faux and not giga_has_leather:
        issues.append({
            "type": "hallucinated_leather",
            "severity": "CRITICAL",
            "detail": "Genuine/Real Leather is optimized but GIGA specifies PU/Faux Leather"
        })
        fixes["__hallucinated_leather__"] = True

    # 4. Solid Wood Hallucination
    opt_has_solid_wood = any("solid wood" in str(x).lower() for x in aspects.get("Material", [])) or bool(re.search(r'\bsolid\s+wood\b', opt_title_l))
    giga_is_engineered = any(re.search(pat, giga_text) for pat in [r'\bmdf\b', r'\bparticle\s*board\b', r'\bcomposite\s+wood\b', r'\bengineered\s+wood\b', r'density board'])
    giga_has_solid_wood = any(re.search(pat, giga_text) for pat in [r'\bsolid\s+wood\b', r'\brubberwood\b', r'\bpine\s+wood\b', r'\boak\s+wood\b'])
    if opt_has_solid_wood and giga_is_engineered and not giga_has_solid_wood:
        issues.append({
            "type": "hallucinated_wood",
            "severity": "CRITICAL",
            "detail": "Solid Wood is optimized but GIGA specifies MDF / Particle Board"
        })
        fixes["__hallucinated_wood__"] = True

    source_material_hint = _extract_source_material_hint(attrs, description or "")
    material_context = " ".join(
        str(value)
        for key, value in aspects.items()
        if key in {"Material", "Frame Material"}
    )
    opt_wood_species = _find_matching_labels(
        f"{opt_title_l} {opt_desc_l} {material_context.lower()}",
        WOOD_SPECIES_PATTERNS,
    )
    giga_wood_species = _find_matching_labels(
        f"{giga_text} {source_material_hint.lower()}",
        WOOD_SPECIES_PATTERNS,
    )
    unsupported_species = [label for label in opt_wood_species if label not in giga_wood_species]
    if unsupported_species:
        issues.append({
            "type": "hallucinated_wood_species",
            "severity": "CRITICAL",
            "detail": (
                f"Listing claims specific wood species ({', '.join(unsupported_species)}) "
                f"but source only supports '{source_material_hint or 'generic wood material'}'"
            ),
        })
        fixes["__hallucinated_wood_species__"] = source_material_hint or "Wood"

    opt_has_cushion = any(re.search(pat, opt_text, flags=re.IGNORECASE) for pat in CUSHION_PATTERNS)
    giga_has_cushion = any(re.search(pat, giga_text, flags=re.IGNORECASE) for pat in CUSHION_PATTERNS) or any(
        re.search(pat, giga_text, flags=re.IGNORECASE) for pat in INHERENT_CUSHION_CONTEXT_PATTERNS
    )
    if opt_has_cushion and not giga_has_cushion:
        issues.append({
            "type": "hallucinated_cushion",
            "severity": "CRITICAL",
            "detail": "Listing mentions cushion/upholstery support but GIGA source does not mention any cushion or padded component",
        })
        fixes["__hallucinated_cushion__"] = True

    opt_has_weather_resistance = any(
        re.search(pat, opt_text, flags=re.IGNORECASE) for pat in WEATHER_RESISTANCE_PATTERNS
    )
    giga_has_weather_resistance = any(
        re.search(pat, giga_text, flags=re.IGNORECASE) for pat in WEATHER_RESISTANCE_PATTERNS
    )
    if opt_has_weather_resistance and not giga_has_weather_resistance:
        issues.append({
            "type": "hallucinated_weather_resistance",
            "severity": "CRITICAL",
            "detail": "Listing claims UV/water/weather resistance but GIGA source does not support any durability rating",
        })
        fixes["__hallucinated_weather_resistance__"] = True

    opt_has_position_count = any(re.search(pat, opt_text, flags=re.IGNORECASE) for pat in POSITION_COUNT_PATTERNS)
    giga_has_position_count = any(re.search(pat, giga_text, flags=re.IGNORECASE) for pat in POSITION_COUNT_PATTERNS)
    if opt_has_position_count and not giga_has_position_count:
        issues.append({
            "type": "hallucinated_position_count",
            "severity": "HIGH",
            "detail": "Listing claims a numbered recline/adjustment position count that GIGA source does not specify",
        })
        fixes["__hallucinated_position_count__"] = True

    # 5. Foldable Hallucination — single arbiter (source_supports_foldable)
    from src.utils.listing_quality_gate import source_supports_foldable as _source_supports_foldable

    giga_has_foldable = _source_supports_foldable(
        source_title=title,
        source_description=description or "",
        attributes=attrs,
        specs=specs,
    )
    # Title-only natural products still skip dual-sided oscillation (umbrella/tent/…)
    is_naturally_foldable = any(
        kw in opt_title_l or kw in giga_title_l
        for kw in ["umbrella", "camping chair", "canopy", "shade sail", "tent", "hammock"]
    )
    if not is_naturally_foldable:
        opt_has_foldable = bool(
            re.search(r"\b(?:foldable|collapsible|foldaway|folding|fold)\b", opt_text)
        ) or any(x in ["Foldable", "Collapsible"] for x in aspects.get("Features", []))
        if opt_has_foldable and not giga_has_foldable:
            issues.append({
                "type": "hallucinated_foldable",
                "severity": "CRITICAL",
                "detail": "Foldable/Collapsible feature is optimized but GIGA source doesn't support folding capability"
            })
            fixes["__hallucinated_foldable__"] = True
        opt_aspects_has_fold = any(x in ["Foldable", "Collapsible"] for x in aspects.get("Features", []))
        if giga_has_foldable and not opt_aspects_has_fold:
            issues.append({
                "type": "missing_foldable",
                "severity": "HIGH",
                "detail": "Foldable/Collapsible feature is supported by GIGA source but missing/removed from listing aspects"
            })
            fixes["__missing_foldable__"] = True

    # 6. Audio Hallucination
    opt_has_audio = any(re.search(pat, opt_text) for pat in [r'\bbluetooth\b', r'\bspeaker\b', r'\baudio\b', r'\bsound\s+system\b'])
    giga_has_audio = any(re.search(pat, giga_text) for pat in [r'\bbluetooth\b', r'\bspeaker\b', r'\baudio\b', r'\bsound\b', r'蓝牙', r'音响', r'喇叭'])
    if opt_has_audio and not giga_has_audio:
        issues.append({
            "type": "hallucinated_audio",
            "severity": "CRITICAL",
            "detail": "Bluetooth speaker or audio system is optimized but GIGA source doesn't mention audio features"
        })
        fixes["__hallucinated_audio__"] = True

    # 7. Pull Rod Material Hallucination (Iron vs Aluminum)
    giga_has_iron = bool(re.search(r'\biron\b', giga_text))
    giga_has_aluminum = bool(re.search(r'\b(?:aluminum|aluminium)\b', giga_text))
    opt_has_aluminum = bool(re.search(r'\b(?:aluminum|aluminium)\b', opt_text))
    if giga_has_iron and not giga_has_aluminum and opt_has_aluminum:
        issues.append({
            "type": "hallucinated_aluminum_rod",
            "severity": "CRITICAL",
            "detail": "Aluminum pull rod/frame is optimized but GIGA specifies Iron"
        })
        fixes["__hallucinated_aluminum_rod__"] = True

    # 8. Tempered Glass Hallucination (Glass vs Tempered Glass)
    opt_has_tempered = bool(re.search(r'\btempered\b', opt_text))
    giga_has_tempered = bool(re.search(r'\btempered\b', giga_text))
    if opt_has_tempered and not giga_has_tempered:
        if "glass" in giga_text or "glass" in opt_text:
            issues.append({
                "type": "hallucinated_tempered_glass",
                "severity": "CRITICAL",
                "detail": "Tempered glass is optimized but GIGA source specifies normal glass"
            })
            fixes["__hallucinated_tempered_glass__"] = True

    # 9. Incomplete title truncation cleanup
    #    Two detectors: the fragment detector (half-words) AND a dangling
    #    connector-word ending (…Table for / …Center with / …End of). The
    #    latter caught 7 of 8 real truncations the fragment detector missed
    #    (2026-07-14 W6018 sweep). Exclude dimension endings ("… 80x60 in").
    _title_dangling = bool(
        re.search(r"\b(with|for|and|to|of|a|an|the|&|featuring a|end of|up)\s*$", opt_title or "", re.I)
    ) and not re.search(r"\d\s*(in|cm|ft|mm|lbs?)\.?\s*$", opt_title or "", re.I)
    if title_has_incomplete_trailing_fragment(opt_title, source_title=title or "") or _title_dangling:
        issues.append({
            "type": "incomplete_title",
            "severity": "MEDIUM",
            "detail": f"Optimized title ends with an incomplete word/connector indicating truncation: '{opt_title}'"
        })
        fixes["__incomplete_title__"] = True

    # 10. Truncated HTML / Bad Material Row Check
    has_bad_material = False
    bad_cell_pat = r'<td\b[^>]*>\s*[A-Z]\s*</td>'
    if re.search(bad_cell_pat, live_description):
        has_bad_material = True
    bad_mat_pat = r'Material</td>\s*<td\b[^>]*>\s*(?:M)?\s*</td>'
    if re.search(bad_mat_pat, live_description, flags=re.IGNORECASE):
        has_bad_material = True
            
    if "extensive product specifications" in live_description.lower() or has_bad_material:
        issues.append({
            "type": "truncated_html_cleanup",
            "severity": "HIGH",
            "detail": "Description contains truncated HTML elements or a blank Material cell"
        })
        fixes["__truncated_html_cleanup__"] = True


    return issues, fixes


def _fill_missing_required_aspects(aspects, category_id, title):
    """Fill in missing required aspects for the category to prevent eBay rejections.
    Uses CATEGORY_REQUIRED_ASPECTS from ebay_publisher + smart title-based detection."""
    from src.services.ebay_publisher import EbayPublisher
    cat_config = EbayPublisher.CATEGORY_REQUIRED_ASPECTS.get(str(category_id), {})
    required = cat_config.get("required", [])
    defaults = cat_config.get("defaults", {})
    title_lower = (title or "").lower()
    type_value = aspects.get("Type")
    type_text = ""
    if isinstance(type_value, list) and type_value:
        type_text = str(type_value[0]).lower()
    elif isinstance(type_value, str):
        type_text = type_value.lower()

    # eBay may require Set Includes for dining-set style categories even when the
    # local category defaults table is incomplete or mismapped.
    existing_set_includes = aspects.get("Set Includes")
    if not existing_set_includes:
        is_dining_set = any(
            kw in title_lower for kw in ["dining set", "table set", "set for", "set of", "5-piece", "5 piece"]
        ) or "dining set" in type_text or "dining table set" in type_text
        if is_dining_set:
            aspects["Set Includes"] = ["Dining Table & Stools" if "stool" in title_lower or "stool" in type_text else "Dining Table & Chairs"]

    for aspect_name in required:
        # Already present and non-empty?
        existing = aspects.get(aspect_name)
        if existing and (isinstance(existing, list) and existing[0]) or (isinstance(existing, str) and existing):
            continue

        if aspect_name == "Set Includes":
            if any(kw in title_lower for kw in ["dining set", "table set", "set for", "set of", "5-piece", "5 piece"]) or "dining set" in type_text:
                if "stool" in title_lower or "stool" in type_text:
                    aspects[aspect_name] = ["Dining Table & Stools"]
                else:
                    aspects[aspect_name] = ["Dining Table & Chairs"]
                continue

        # Smart detection based on title
        detected = _detect_aspect_from_title(aspect_name, title_lower)
        if detected:
            aspects[aspect_name] = [detected] if not isinstance(detected, list) else detected
        elif aspect_name in defaults:
            aspects[aspect_name] = [defaults[aspect_name]]

    return aspects


def _detect_aspect_from_title(aspect_name, title_lower):
    """Detect an aspect value from the product title."""
    if aspect_name in {"Number of Items in Set", "Number of Pieces"}:
        return infer_number_of_items_in_set(title_lower)
    if aspect_name == "Compatible Mattress Size" or aspect_name == "Size":
        for size in ["California King", "King", "Queen", "Full", "Twin XL", "Twin"]:
            if size.lower() in title_lower:
                return size
        return None
    if aspect_name == "Voltage":
        if any(kw in title_lower for kw in ["power outlet", "electric", "120v", "110v"]):
            return "120 V"
        return "Does Not Apply"
    if aspect_name == "Upholstery Fabric":
        fabric_map = {
            "corduroy": "Corduroy",
            "velvet": "Velvet",
            "linen": "Linen",
            "faux leather": "Faux Leather",
            "pu leather": "Faux Leather",
            "polyurethane": "Faux Leather",
            "leather": "Faux Leather",
            "polyester": "Polyester",
            "microfiber": "Microfiber",
            "fabric": "Fabric",
            "foam": "Foam",
        }
        for kw, val in fabric_map.items():
            if kw in title_lower:
                return val
        if " pu " in f" {title_lower} " or title_lower.endswith(" pu"):
            return "Faux Leather"
        if "mattress" in title_lower:
            return "Foam"
        if "cooler" in title_lower:
            return "Does Not Apply"
        return "Fabric"
    if aspect_name == "Set Includes":
        if "dining" in title_lower and "table" in title_lower:
            return "Dining Table"
        if "set" in title_lower:
            return "See Description"
        return "See Description"
    if aspect_name == "Firmness":
        for level in ["Firm", "Plush", "Soft", "Medium Firm", "Medium"]:
            if level.lower() in title_lower:
                return level
        return "Medium"
    if aspect_name == "Item Height":
        return None  # Let dimension extraction handle this
    return None


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


def build_live_listing_opt_snapshot(stored_opt, *, inventory_item=None, offer=None):
    """Build an optimization-shaped payload from live eBay inventory/offer data."""
    opt = dict(stored_opt or {})
    product = (inventory_item or {}).get("product") or {}
    offer = offer or {}

    live_title = product.get("title")
    if live_title:
        opt["title"] = live_title

    live_description = offer.get("listingDescription") or product.get("description")
    if live_description is not None:
        opt["description"] = live_description

    live_aspects = product.get("aspects")
    if isinstance(live_aspects, dict):
        opt["aspects"] = live_aspects

    category = offer.get("category") or {}
    category_id = offer.get("categoryId") or category.get("categoryId")
    if category_id:
        opt["categoryId"] = str(category_id)
    category_name = category.get("categoryName") or category.get("name")
    if category_name:
        opt["categoryName"] = category_name

    return opt


def create_parser():
    parser = argparse.ArgumentParser(description="Audit & fix active eBay listings")
    parser.add_argument("--fix", action="store_true", help="Apply fixes to eBay (default: dry run)")
    parser.add_argument("--sku", type=str, help="Audit or fix a specific SKU only")
    parser.add_argument("--sku-file", help="Optional text file with one SKU per line")
    parser.add_argument(
        "--source-report",
        help="Prior audit JSON used to select affected SKUs; requires --issue-type",
    )
    parser.add_argument(
        "--issue-type",
        action="append",
        dest="issue_types",
        help="Issue type to select from --source-report (repeatable)",
    )
    parser.add_argument(
        "--fix-key",
        action="append",
        dest="fix_keys",
        help="Only apply these generated fix keys (repeatable; audit remains unchanged)",
    )
    parser.add_argument(
        "--local-video-status",
        action="append",
        dest="local_video_statuses",
        help="For a report-scoped run, keep only SKUs with a local video ID in this status (repeatable)",
    )
    parser.add_argument("--limit", type=int, help="Optional max number of published rows to audit")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Read live eBay inventory/offer content before comparing it with GIGA source data",
    )
    parser.add_argument("--email", action="store_true", help="Send a Chinese audit summary email")
    parser.add_argument(
        "--record-clean-state",
        action="store_true",
        help="Persist local clean/dirty freeze metadata during live audits",
    )
    parser.add_argument(
        "--ignore-clean-freeze",
        action="store_true",
        help="Force a full live audit even when source/live fingerprints already match a verified clean state",
    )
    parser.add_argument(
        "--exit-zero-on-issues",
        action="store_true",
        help="Exit 0 when the audit completes even if mismatches are found",
    )
    parser.add_argument("--report", help="Optional custom JSON report path")
    return parser


def is_full_published_scope(args) -> bool:
    return not args.sku and not args.sku_file and not args.source_report


def get_report_source(args) -> str:
    return "live_ebay" if args.live else "local_db_optimization"


def validate_fix_scope(args, parser) -> None:
    if args.source_report and (args.sku or args.sku_file):
        parser.error("--source-report cannot be combined with --sku or --sku-file")
    if args.source_report and not args.issue_types:
        parser.error("--source-report requires at least one --issue-type")
    if args.issue_types and not args.source_report:
        parser.error("--issue-type requires --source-report")
    if args.local_video_statuses and not args.source_report:
        parser.error("--local-video-status requires --source-report")
    if args.fix and not args.live and is_full_published_scope(args):
        parser.error(
            "--fix on full published inventory requires --live; "
            "use --live or scope the run with --sku/--sku-file"
        )
    if args.record_clean_state and not args.live:
        parser.error("--record-clean-state requires --live")
    if args.ignore_clean_freeze and not args.live:
        parser.error("--ignore-clean-freeze requires --live")


def _fetch_live_listing_context(ebay_client, sku, expected_listing_id=None):
    inventory_item = ebay_client.get_inventory_item(sku) or {}
    offers = ebay_client.get_offers_by_sku(sku) or []
    offer = _select_best_offer(offers, expected_listing_id=expected_listing_id)
    return inventory_item, offer


def _load_skus_from_file(path: str) -> list[str]:
    values = []
    seen = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        sku = line.strip()
        if not sku or sku.startswith("#"):
            continue
        if sku in seen:
            continue
        seen.add(sku)
        values.append(sku)
    return values


def _load_skus_from_audit_report(path: str, issue_types: set[str]) -> list[str]:
    """Return unique SKUs whose prior audit includes one requested issue type.

    This deliberately scopes a repair run to a saved audit snapshot. The
    listing is still read live before any update, so a resolved or changed
    issue does not result in a blind write.
    """
    requested = {str(issue_type or "").strip() for issue_type in issue_types}
    requested.discard("")
    if not requested:
        return []

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read audit report {path}: {exc}") from exc

    entries = payload.get("issues", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        raise ValueError(f"audit report {path} has invalid issues payload")

    skus = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sku = str(entry.get("sku") or "").strip()
        issues = entry.get("issues") or []
        matched = any(
            isinstance(issue, dict) and str(issue.get("type") or "").strip() in requested
            for issue in issues
        )
        if sku and matched and sku not in seen:
            seen.add(sku)
            skus.append(sku)
    return skus


def filter_fixes_by_key(fixes: dict, allowed_keys: set[str] | None) -> dict:
    """Return only explicitly approved fixes without mutating the audit result."""
    if not allowed_keys:
        return dict(fixes or {})
    allowed = {str(key) for key in allowed_keys}
    return {key: value for key, value in (fixes or {}).items() if key in allowed}


def filter_skus_by_local_video_status(
    db_conn,
    skus: list[str],
    allowed_statuses: set[str],
) -> list[str]:
    """Keep report-selected SKUs with a reusable locally recorded eBay video ID."""
    wanted = {str(status or "").strip().upper() for status in allowed_statuses}
    wanted.discard("")
    if not skus or not wanted:
        return list(skus)

    placeholders = ",".join("?" for _ in skus)
    rows = db_conn.execute(
        f"SELECT sku, optimization FROM collected_products WHERE sku IN ({placeholders})",
        tuple(skus),
    ).fetchall()
    eligible = set()
    for row in rows:
        try:
            sku = row["sku"]
            optimization = row["optimization"]
        except (IndexError, TypeError):
            sku, optimization = row[0], row[1]
        opt = parse_json(optimization)
        status = str(opt.get("video_status") or opt.get("videoStatus") or "").strip().upper()
        video_id = str(opt.get("video_id") or opt.get("videoId") or "").strip()
        if not video_id:
            video_ids = opt.get("videoIds")
            if isinstance(video_ids, list) and video_ids:
                video_id = str(video_ids[0] or "").strip()
        if status in wanted and video_id:
            eligible.add(sku)
    return [sku for sku in skus if sku in eligible]


def _render_audit_issue_items(issues, *, limit=5):
    rendered = []
    for issue in (issues or [])[:limit]:
        rendered.append(
            "<li><strong>{sev}</strong> {typ}: {detail}</li>".format(
                sev=html.escape(str(issue.get("severity", ""))),
                typ=html.escape(str(issue.get("type", issue.get("field", "")))),
                detail=html.escape(str(issue.get("detail", issue.get("issue", "")))[:260]),
            )
        )
    return "\n".join(rendered) or "<li>无</li>"


def _render_audit_fix_items(fixes, *, limit=10):
    rendered = []
    for fix in (fixes or [])[:limit]:
        rendered.append(f"<li>{html.escape(str(fix)[:260])}</li>")
    if fixes and len(fixes) > limit:
        rendered.append(f"<li>其余 {len(fixes) - limit} 项修复见附件报告</li>")
    return "\n".join(rendered) or "<li>无自动修复</li>"


def _render_audit_table_rows(items, *, include_fixes=False, max_rows=200):
    rows = []
    truncated = False
    for item in items[:max_rows]:
        row_cells = [
            html.escape(str(item.get("sku", ""))),
            html.escape(str(item.get("listing_id", ""))),
            html.escape(str(item.get("title", ""))[:120]),
            f'<ul style="margin:0;padding-left:18px;">{_render_audit_issue_items(item.get("issues", []))}</ul>',
        ]
        if include_fixes:
            row_cells.append(
                f'<ul style="margin:0;padding-left:18px;">{_render_audit_fix_items(item.get("fixes_applied", []))}</ul>'
            )
        rows.append(
            "<tr>{cells}</tr>".format(
                cells="".join(
                    f'<td style="padding:8px;border-bottom:1px solid #ddd;vertical-align:top;">{cell}</td>'
                    for cell in row_cells
                )
            )
        )
    if len(items) > max_rows:
        truncated = True
    return "\n".join(rows), truncated


def _send_audit_email(report, report_path):
    from src.utils.email_sender import send_email

    total_published = int(report.get("total_published", 0))
    total_with_issues = int(report.get("total_with_issues", 0))
    total_transport_failures = int(report.get("total_transport_failures", 0))
    fixed_count = int(report.get("fixed_count", 0))
    mode = str(report.get("mode", ""))
    severity_counts = report.get("severity_counts", {})
    issues = report.get("issues", [])
    transport_issues = report.get("transport_issues", [])
    fixed_items = [item for item in issues if item.get("fixes_applied")]
    manual_items = [item for item in issues if not item.get("fixes_applied")]

    summary = f"检查 {total_published} 条，发现 {total_with_issues} 条 listing 有内容问题"
    if total_transport_failures:
        summary = f"{summary}，另有 {total_transport_failures} 条抓取/availability 异常"
    if mode == "fix":
        summary = f"{summary}，已修复 {fixed_count} 条"
        subject = f"eBay/GIGA 刊登修复报告 - {summary}"
    else:
        subject = f"eBay/GIGA 刊登内容审计 - {summary}"

    fixed_rows, fixed_rows_truncated = _render_audit_table_rows(fixed_items, include_fixes=True)
    manual_rows, manual_rows_truncated = _render_audit_table_rows(manual_items, include_fixes=False)
    issue_rows, issue_rows_truncated = _render_audit_table_rows(issues, include_fixes=False)

    fixed_section = ""
    if mode == "fix":
        fixed_note = (
            "<p style='color:#666;'>邮件正文最多展示前 200 条自动修复记录，其余请看附件报告。</p>"
            if fixed_rows_truncated
            else ""
        )
        fixed_section = """
        <h3>已自动修复明细</h3>
        <p>共 {fixed_count} 条。</p>
        {fixed_note}
        <table style="border-collapse:collapse;width:100%;">
          <thead>
            <tr>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">SKU</th>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">Listing ID</th>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">Title</th>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">Issues</th>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">Fixes Applied</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        """.format(
            fixed_count=fixed_count,
            fixed_note=fixed_note,
            rows=fixed_rows or '<tr><td colspan="5" style="padding:8px;">本次没有自动修复记录</td></tr>',
        )

    manual_title = "待人工复核" if mode == "fix" else "优先检查 SKU"
    manual_note = (
        "<p style='color:#666;'>邮件正文最多展示前 200 条待复核记录，其余请看附件报告。</p>"
        if ((manual_rows_truncated and mode == "fix") or (issue_rows_truncated and mode != "fix"))
        else ""
    )
    manual_rows_html = manual_rows if mode == "fix" else issue_rows
    transport_rows, transport_rows_truncated = _render_audit_table_rows(
        transport_issues,
        include_fixes=False,
    )
    transport_section = ""
    if transport_issues:
        transport_note = (
            "<p style='color:#666;'>邮件正文最多展示前 200 条抓取异常记录，其余请看附件报告。</p>"
            if transport_rows_truncated
            else ""
        )
        transport_section = """
        <h3>抓取 / Availability 异常</h3>
        <p>共 {transport_count} 条。这些不计入内容质检错误数，但需要复跑或排查 eBay API 可用性。</p>
        {transport_note}
        <table style="border-collapse:collapse;width:100%;">
          <thead>
            <tr>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">SKU</th>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">Listing ID</th>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">Title</th>
              <th align="left" style="padding:8px;border-bottom:2px solid #999;">Issues</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        """.format(
            transport_count=total_transport_failures,
            transport_note=transport_note,
            rows=transport_rows,
        )

    html_body = """
    <h2>eBay/GIGA 刊登内容审计</h2>
    <p>{summary}</p>
    <ul>
      <li>模式: {mode}</li>
      <li>CRITICAL: {critical}</li>
      <li>HIGH: {high}</li>
      <li>MEDIUM: {medium}</li>
      <li>LOW: {low}</li>
      <li>抓取 / Availability 异常: {transport_failures}</li>
      <li>报告文件: <code>{report_path}</code></li>
    </ul>
    {fixed_section}
    <h3>{manual_title}</h3>
    {manual_note}
    <table style="border-collapse:collapse;width:100%;">
      <thead>
        <tr>
          <th align="left" style="padding:8px;border-bottom:2px solid #999;">SKU</th>
          <th align="left" style="padding:8px;border-bottom:2px solid #999;">Listing ID</th>
          <th align="left" style="padding:8px;border-bottom:2px solid #999;">Title</th>
          <th align="left" style="padding:8px;border-bottom:2px solid #999;">Issues</th>
        </tr>
      </thead>
      <tbody>{manual_rows}</tbody>
    </table>
    {transport_section}
    """.format(
        summary=html.escape(summary),
        mode=html.escape(mode),
        critical=int(severity_counts.get("CRITICAL", 0)),
        high=int(severity_counts.get("HIGH", 0)),
        medium=int(severity_counts.get("MEDIUM", 0)),
        low=int(severity_counts.get("LOW", 0)),
        transport_failures=total_transport_failures,
        report_path=html.escape(str(report_path)),
        fixed_section=fixed_section,
        manual_title=manual_title,
        manual_note=manual_note,
        manual_rows=manual_rows_html or '<tr><td colspan="4" style="padding:8px;">未发现问题</td></tr>',
        transport_section=transport_section,
    )
    return send_email(subject, html_body, attachments=[str(report_path)])


def _put_inventory_product_only(ebay_client, sku, title, description, aspects):
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
    sanitize_single_value_aspects(
        cleaned_aspects,
        log=lambda message: logging.info(f"[SANITIZE] {sku}: {message}"),
        multi_value_aspects={key for key in cleaned_aspects if key not in forced_single},
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

    payload = {
        "condition": live_inventory.get("condition") or "NEW",
        "product": product,
    }
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

    token = ebay_client.oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }
    url = f"{ebay_client.base_url}/sell/inventory/v1/inventory_item/{sku}"
    response = ebay_client.session.put(url, headers=headers, json=payload, timeout=120)
    if response.status_code not in (200, 204):
        raise RuntimeError(f"{sku}: product-only inventory update failed {response.status_code}: {response.text[:300]}")

    return response, description, cleaned_aspects


def rebuild_specifications_table(attrs, specs, aspects, assembly_required="No"):
    from src.utils.dimension_helpers import extract_all_dimensions
    dims = extract_all_dimensions(attrs)
    dim_str = ""
    if dims.get("length") and dims.get("width") and dims.get("height"):
        dim_str = f"{dims['length']} × {dims['width']} × {dims['height']} inches"
        
    weight_str = ""
    if dims.get("weight"):
        weight_str = f"{dims['weight']} lbs"
        
    material_val = ""
    if "Material" in aspects:
        material_val = aspects["Material"][0] if isinstance(aspects["Material"], list) else str(aspects["Material"])
    if not material_val and "Material" in attrs:
        material_val = str(attrs["Material"])
    if not material_val and "Variant" in attrs:
        parts = str(attrs["Variant"]).split("+")
        if len(parts) >= 2:
            material_val = parts[1].strip()
            
    assembly_str = "No"
    if assembly_required == "Yes":
        assembly_str = "Yes - Hardware and instructions included; setup required before use."
        
    rows = []
    if dim_str:
        rows.append(f'<tr><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72;width:40%">Overall Dimensions (L×W×H)</td><td style="padding:10px;border-bottom:1px solid #e0e0e0;font-weight:500">{dim_str}</td></tr>')
    if weight_str:
        rows.append(f'<tr style="background:#fafafa"><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Weight</td><td style="padding:10px;border-bottom:1px solid #e0e0e0;font-weight:500">{weight_str}</td></tr>')
    
    bg_style = ' style="background:#fafafa"' if len(rows) % 2 == 1 else ''
    if material_val:
        rows.append(f'<tr{bg_style}><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Material</td><td style="padding:10px;border-bottom:1px solid #e0e0e0;font-weight:500">{material_val}</td></tr>')
        
    bg_style = ' style="background:#fafafa"' if len(rows) % 2 == 1 else ''
    rows.append(f'<tr{bg_style} data-assembly-note="true"><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72;width:40%">Assembly Required</td><td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#2d3436">{assembly_str}</td></tr>')
    
    table_rows = "\n".join(rows)
    
    html = f'''<!-- Specifications Table --><div style="padding:25px;background:#fff"><h3 style="margin:0 0 15px;font-size:16px;color:#0d1b2a;border-left:4px solid #d4af37;padding-left:12px">SPECIFICATIONS</h3><table style="width:100%;border-collapse:collapse">{table_rows}</table></div>'''
    return html


def build_structured_description_from_source(title, source_description, attrs, specs, aspects):
    feature_bullets = extract_source_feature_bullets(source_description)
    if not feature_bullets:
        return ""

    assembly_required = first_aspect_text(aspects, "Assembly Required") or infer_source_assembly_required(
        attrs,
        specs,
        source_description,
    ) or "No"
    bullet_html = "".join(
        f'<li style="margin-bottom:10px">{html.escape(bullet)}</li>'
        for bullet in feature_bullets
    )
    perfect_for = html.escape(_build_perfect_for_copy(title, source_description, aspects))
    package_includes = html.escape(
        _build_package_includes_copy(title, source_description, aspects, assembly_required)
    )
    specifications_html = rebuild_specifications_table(
        attrs,
        specs,
        aspects,
        assembly_required=assembly_required,
    )

    return (
        '<div style="max-width:900px;margin:0 auto;font-family:Arial,sans-serif;color:#1a1a1a;line-height:1.7">'
        '<div style="text-align:center;padding:30px 15px;background:linear-gradient(135deg,#0d1b2a 0%,#1a365d 100%)">'
        '<h1 style="margin:0;font-size:28px;font-weight:300;letter-spacing:6px;color:#d4af37">AQUAVERVE</h1>'
        '<p style="margin:8px 0 0;font-size:12px;color:#a0a0a0;letter-spacing:2px">PREMIUM HOME FURNISHINGS</p>'
        '</div>'
        '<div style="background:#f8f9fa;padding:25px;text-align:center;border-bottom:2px solid #d4af37">'
        f'<h2 style="margin:0;font-size:20px;color:#2d3436;font-weight:500">{html.escape(title)}</h2>'
        '</div>'
        '<div style="padding:25px">'
        '<h3 style="margin:0 0 15px;font-size:16px;color:#0d1b2a;border-left:4px solid #d4af37;padding-left:12px">KEY FEATURES</h3>'
        f'<ul style="margin:0;padding-left:20px;color:#4a4a4a">{bullet_html}</ul>'
        '</div>'
        '<div style="padding:20px 25px;background:#f0f4f8">'
        '<h3 style="margin:0 0 12px;font-size:14px;color:#0d1b2a">PERFECT FOR</h3>'
        f'<p style="margin:0;color:#636e72">{perfect_for}</p>'
        '</div>'
        f'{specifications_html}'
        '<div style="padding:20px 25px;background:#f8f9fa;border-top:1px solid #e0e0e0">'
        '<h3 style="margin:0 0 10px;font-size:14px;color:#0d1b2a">PACKAGE INCLUDES</h3>'
        f'<p style="margin:0;color:#636e72">{package_includes}</p>'
        '</div>'
        '<div style="text-align:center;padding:20px;background:linear-gradient(135deg,#0d1b2a 0%,#1a365d 100%)">'
        '<p style="margin:0;font-size:12px;color:#d4af37;letter-spacing:1px">✦ Ships from US Warehouse ✦</p>'
        '<p style="margin:8px 0 0;font-size:11px;color:#808080">Quality Guaranteed • Fast US Shipping • Trusted Seller</p>'
        '</div>'
        '</div>'
    )


def _ensure_live_video_id_attached(ebay_client, uploader, sku: str, video_id: str) -> bool:
    """Ensure the uploaded video id is present on the live inventory product."""
    target = str(video_id or "").strip()
    if not target:
        return False

    for attempt in range(3):
        try:
            live_inventory = ebay_client.get_inventory_item(sku) or {}
            live_video_ids = parse_image_list(((live_inventory.get("product") or {}).get("videoIds") or []))
            if target in {str(item).strip() for item in live_video_ids}:
                return True
        except Exception as exc:
            logging.warning(f"[AUDIT] {sku}: failed to verify live videoIds: {exc}")

        if attempt == 0:
            try:
                if not uploader._add_video_to_ebay_inventory(sku, target):
                    logging.warning(f"[AUDIT] {sku}: eBay inventory video attach returned false for {target}")
            except Exception as exc:
                logging.warning(f"[AUDIT] {sku}: failed to attach video {target}: {exc}")

        time.sleep(1)

    return False


def _refresh_source_video_url(sku: str, fallback_url: str) -> str:
    """Fetch a freshly signed GigaB2B video URL for *sku* when possible.

    DB-stored videos often carry expired ``x-ct`` signature params. Returns
    *fallback_url* unchanged when credentials/API/snapshot are unavailable.
    """
    fallback = str(fallback_url or "").strip()
    try:
        from src.clients.dajian_client import DaJianClient
        from src.services.source_refresh import build_source_snapshot

        client_id = os.getenv("DAJIAN_API_KEY")
        client_secret = os.getenv("DAJIAN_API_SECRET")
        if not client_id or not client_secret:
            return fallback
        client = DaJianClient(client_id, client_secret)
        detail = client.get_product_detail_by_sku(sku)
        snapshot = build_source_snapshot(detail or {})
        if snapshot and snapshot.videos:
            fresh = str(snapshot.videos[0] or "").strip()
            if fresh:
                return fresh
    except Exception as exc:
        logging.warning(f"[AUDIT] {sku}: fresh video URL refresh failed: {exc}")
    return fallback


def _stored_video_id_from_optimization(opt: dict) -> str:
    if not isinstance(opt, dict):
        return ""
    video_id = str(opt.get("video_id") or "").strip()
    if video_id:
        return video_id
    video_ids = opt.get("videoIds")
    if isinstance(video_ids, list) and video_ids:
        return str(video_ids[0] or "").strip()
    if isinstance(video_ids, str):
        return video_ids.strip()
    return ""


def _remove_live_video_ids_from_inventory(ebay_client, sku: str) -> bool:
    live_inventory = ebay_client.get_inventory_item(sku) or {}
    product = live_inventory.get("product") or {}
    if not product:
        return False

    quantity = (
        ((live_inventory.get("availability") or {}).get("shipToLocationAvailability") or {})
        .get("quantity")
    )
    from src.utils.ebay_quantity import normalize_ebay_listing_quantity

    payload = {
        "title": product.get("title") or "",
        "description": product.get("description") or "",
        "image_urls": list(product.get("imageUrls") or []),
        "video_urls": [],
        "price": 99.99,
        "quantity": normalize_ebay_listing_quantity(quantity or 1),
        "condition": live_inventory.get("condition") or "NEW",
        "aspects": dict(product.get("aspects") or {}),
    }
    if live_inventory.get("packageWeightAndSize"):
        payload["packageWeightAndSize"] = live_inventory.get("packageWeightAndSize")

    ebay_client.create_or_replace_inventory_item(sku=sku, product=payload)
    return True


def fix_listing_on_ebay(sku, product_row, fixes, ebay_client, db_conn, *, base_opt_raw=None):
    """Apply fixes to a published eBay listing."""
    results = []
    stored_opt = parse_json(product_row["optimization"])
    opt = parse_json(base_opt_raw if base_opt_raw is not None else product_row["optimization"])
    aspects = opt.get("aspects", {}) if isinstance(opt, dict) else {}
    title = opt.get("title", product_row["title"] or "")
    description = opt.get("description", product_row["description"] or "")
    source_title = product_row.get("title", "") if isinstance(product_row, dict) else product_row["title"]
    source_description = (
        product_row.get("description", "") if isinstance(product_row, dict) else product_row["description"]
    ) or ""
    attrs = parse_json(product_row["attributes"])
    specs_raw = product_row.get("specs") if isinstance(product_row, dict) else product_row["specs"]
    source_specs = parse_json(specs_raw) if specs_raw else {}

    aspect_changed = False
    category_changed = False
    description_changed = False
    title_changed = False
    new_category = None
    new_category_name = None
    compatibility_payload = None
    image_restore_requested = False
    offers = []
    live_offer = None
    live_offer_category = None
    product_only_update = False
    needs_inventory_update = False
    republished_listing_id = None
    video_changed = False
    synced_video_id = None
    reused_existing_live_video = False
    video_removed = False

    # Apply aspect fixes
    for key, value in fixes.items():
        if key == "categoryId":
            new_category = value
            category_changed = True
            continue
        if key == "categoryName":
            new_category_name = value
            continue
        if key == "__motors_compatibility__":
            compatibility_payload = value
            results.append(f"Motors compatibility metadata refreshed ({value.get('mode', 'unknown')})")
            continue
        if key == "__restore_images__":
            image_restore_requested = bool(value)
            continue
        if key == "__sync_video__":
            from src.services.ebay_video_uploader import EbayVideoUploader

            uploader = EbayVideoUploader(ebay_client.oauth)
            existing_video_id = _stored_video_id_from_optimization(stored_opt)
            if existing_video_id and _ensure_live_video_id_attached(ebay_client, uploader, sku, existing_video_id):
                synced_video_id = existing_video_id
                video_changed = True
                reused_existing_live_video = True
                results.append(f"Re-linked existing source video on eBay: {existing_video_id}")
                continue

            # Prefer a freshly signed GigaB2B video URL (DB snapshots often carry
            # expired x-ct query params). Fall back to the stored URL on failure.
            stored_video_url = str(value or "").strip()
            video_url = _refresh_source_video_url(sku, stored_video_url)
            if video_url != stored_video_url:
                results.append(f"Refreshed source video URL for {sku} before upload")

            video_id = uploader.upload_video_sync(
                video_url,
                sku,
                title or source_title or "Product Video",
            )
            if video_id and _ensure_live_video_id_attached(ebay_client, uploader, sku, video_id):
                synced_video_id = video_id
                video_changed = True
                results.append(f"Uploaded and linked source video to eBay: {video_id}")
            else:
                err = str(getattr(uploader, "last_upload_error", "") or "")
                if err.startswith("unsupported_source") or "not a direct downloadable" in err:
                    # Source is permanently unusable for eBay (e.g. .txt placeholder)
                    results.append(f"ERROR: video_source_dead for {sku}: {err}")
                else:
                    results.append(f"ERROR: Failed to upload/link source video for {sku}")
            continue
        if key == "__remove_video__":
            if _remove_live_video_ids_from_inventory(ebay_client, sku):
                video_removed = True
                results.append("Removed stale live eBay videoIds")
            else:
                results.append(f"ERROR: Failed to remove stale live eBay videoIds for {sku}")
            continue
        if key == "__assembly_desc_update__":
            updated_description = rewrite_assembly_copy(description, value)
            if updated_description != description:
                description = updated_description
                description_changed = True
                results.append(f"Description assembly wording updated to match Assembly Required={value}")
            continue
        if key == "__title__":
            cleaned_title, _ = normalize_listing_title_for_ebay(value, source_title=source_title or "")
            if cleaned_title and cleaned_title != title:
                title = cleaned_title
                title_changed = True
                results.append(f"Cleaned title: {title}")
            continue
        if key == "__sanitize_live_description_html__":
            sanitized_description = sanitize_generated_description_html(description)
            if sanitized_description != description:
                description = sanitized_description
                description_changed = True
                results.append("Live description HTML artifacts sanitized")
            continue
        if key == "__rebuild_description_from_source__":
            # W6018 accident closure: rebuild store template from fresh source.
            # Reuse repair_broken_listings (do not duplicate rebuild logic).
            try:
                from scripts.repair_broken_listings import _dajian, build_repair, verify

                dj = _dajian()
                built, reason = build_repair(db_conn, dj, sku)
                if built is None:
                    results.append(
                        f"ERROR [{sku}]: source rebuild skipped ({reason or 'unknown'})"
                    )
                    continue
                new_title, new_desc, new_aspects, snap = built
                v = verify(new_title, new_desc, new_aspects, snap)
                if not v.get("passed"):
                    results.append(
                        f"ERROR [{sku}]: source rebuild failed verification "
                        f"(claim_critical={v.get('claim_critical')}, "
                        f"markers_ok={v.get('markers_ok')}, title_ok={v.get('title_ok')}) — not pushed"
                    )
                    continue
                title = new_title
                title_changed = True
                description = sanitize_generated_description_html(new_desc)
                description_changed = True
                aspects = dict(new_aspects or aspects)
                aspect_changed = True
                results.append(
                    "Rebuilt store template + word-safe title from source "
                    "(raw-source dump / broken listing guard)"
                )
            except Exception as exc:
                results.append(f"ERROR [{sku}]: source rebuild exception: {exc}")
            continue
        if key == "__restore_live_description_from_local__":
            stored_opt = parse_json(product_row["optimization"])
            stored_desc = sanitize_generated_description_html(
                stored_opt.get("description", "") if isinstance(stored_opt, dict) else ""
            )
            if description_has_key_features_template(stored_desc):
                description = stored_desc
                description_changed = True
                results.append("Live description restored from local KEY FEATURES template")
            else:
                rebuilt_description = build_structured_description_from_source(
                    title or source_title or "",
                    source_description,
                    attrs,
                    source_specs,
                    aspects,
                )
                if rebuilt_description:
                    description = sanitize_generated_description_html(rebuilt_description)
                    description_changed = True
                    results.append("Live description rebuilt from source product features")
                else:
                    results.append(f"ERROR [{sku}]: no valid local or source template available to rebuild description")
            continue
        if key == "__desc_needs_update__":
            # Re-generate correct dimension string in description
            dims = extract_all_dimensions(attrs)
            updated_description = replace_description_measurements(
                description,
                length=dims.get("length"),
                width=dims.get("width"),
                height=dims.get("height"),
                weight=dims.get("weight"),
            )
            if updated_description != description:
                description = updated_description
                description_changed = True
                if dims.get("length") and dims.get("width") and dims.get("height"):
                    results.append(
                        f"Description dimensions updated to {dims['length']} × {dims['width']} × {dims['height']} inches"
                    )
                if dims.get("weight"):
                    results.append(f"Description weight updated to {dims['weight']} lbs")
            elif dims.get("length") and dims.get("width") and dims.get("height"):
                assembly_required = first_aspect_text(aspects, "Assembly Required") or infer_source_assembly_required(
                    attrs,
                    source_specs,
                    source_description,
                )
                rebuilt_table = rebuild_specifications_table(
                    attrs,
                    source_specs,
                    aspects,
                    assembly_required=assembly_required or "No",
                )
                description = replace_specifications_table_html(description, rebuilt_table)
                description_changed = True
                results.append("Description specifications table rebuilt from source dimensions/weight")
            continue

        if key == "__claim_diff_violations__":
            for claim_text in value:
                normalized_claim = str(claim_text or "").strip()
                claimed_match = re.search(
                    r"claimed\s+(.+?)(?:,\s*source\s+has\s+.+)?$",
                    normalized_claim,
                    flags=re.IGNORECASE,
                )
                if claimed_match:
                    normalized_claim = claimed_match.group(1).strip()
                claim_pattern = build_claim_cleanup_pattern(claim_text)

                for aspect_key, aspect_val in list(aspects.items()):
                    key_patterns = CLAIM_SPECIFIC_ASPECT_KEY_PATTERNS.get(normalized_claim.casefold(), ())
                    if key_patterns and any(
                        re.search(pattern, aspect_key, flags=re.IGNORECASE)
                        for pattern in key_patterns
                    ):
                        aspects.pop(aspect_key, None)
                        aspect_changed = True
                        results.append(f"Aspect {aspect_key} removed (claim violation)")
                        continue

                    original_list = aspect_val if isinstance(aspect_val, list) else [aspect_val]
                    new_list = []
                    for v in original_list:
                        new_v = re.sub(claim_pattern, "", str(v), flags=re.IGNORECASE)
                        new_v = re.sub(r"\s+", " ", new_v).strip(" ,-/|")
                        if new_v:
                            new_list.append(new_v)
                    if not new_list:
                        aspects.pop(aspect_key)
                        aspect_changed = True
                        results.append(f"Aspect {aspect_key} removed (claim violation)")
                    elif new_list != [str(x) for x in original_list]:
                        aspects[aspect_key] = new_list
                        aspect_changed = True
                        results.append(f"Aspect {aspect_key} edited (claim violation)")

                if claim_text == "cushion":
                    for _cush_key in ("Upholstery Material", "Upholstery Fabric", "Cushion Color", "Fill Material"):
                        if _cush_key in aspects:
                            aspects.pop(_cush_key)
                            aspect_changed = True
                            results.append(f"Aspect {_cush_key} removed (cushion hallucination)")

                new_title = re.sub(claim_pattern, "", title, flags=re.IGNORECASE)
                new_title = re.sub(r"\s+", " ", new_title).strip(" ,-/|")
                if new_title != title:
                    title = new_title
                    title_changed = True
                    results.append(f"Title removed claim violation: {claim_text}")

                new_description = re.sub(claim_pattern, "", description, flags=re.IGNORECASE)
                if new_description != description:
                    description = new_description
                    description_changed = True
                    results.append(f"Description removed claim violation: {claim_text}")
            continue

        if key == "__hallucinated_tsa__":
            if "Features" in aspects:
                aspects["Features"] = [f for f in aspects["Features"] if not any(w in str(f).lower() for w in ["tsa", "transportation security administration"])]
                aspect_changed = True
            new_title = re.sub(r'\b(?:TSA|Transportation\s+Security\s+Administration)\b\s*', '', title, flags=re.IGNORECASE)
            new_title = re.sub(r'\s+', ' ', new_title).strip()
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title TSA lock reference removed: {title}")
            new_description = re.sub(r'\b(?:TSA-Approved|TSA approved|TSA|Transportation\s+Security\s+Administration)\b', 'Secure', description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description TSA lock references removed")
            continue
        if key == "__hallucinated_charging__":
            # Strip USB/power tokens from title, description, AND all aspects
            # (W2700 residual was Type="... with Power Strip", not Features/description).
            charging_token_re = re.compile(
                r"\b(?:usb(?:[-\s]?c)?|charging\s+stations?|charging\s+ports?|"
                r"power\s+outlets?|power\s+strips?|built-in\s+outlets?|chargers?)\b",
                flags=re.IGNORECASE,
            )

            def _scrub_charging_text(text: str) -> str:
                cleaned = charging_token_re.sub(" ", str(text or ""))
                cleaned = re.sub(r"\s{2,}", " ", cleaned)
                cleaned = re.sub(r"\s+([,.;:/])", r"\1", cleaned)
                cleaned = re.sub(r"(\s+with|\s+and|\s+or)\s*$", "", cleaned, flags=re.IGNORECASE)
                return cleaned.strip(" -/,;|")

            for aspect_key, aspect_val in list(aspects.items()):
                vals = aspect_val if isinstance(aspect_val, list) else [aspect_val]
                new_vals = []
                for raw in vals:
                    scrubbed = _scrub_charging_text(str(raw))
                    if not scrubbed:
                        continue
                    # Drop pure charging feature labels
                    if aspect_key == "Features" and re.fullmatch(
                        r"(?:usb|charging|charger|outlet|power\s+strip|power\s+outlet)s?",
                        scrubbed,
                        flags=re.IGNORECASE,
                    ):
                        continue
                    new_vals.append(scrubbed)
                if new_vals != list(vals):
                    if new_vals:
                        aspects[aspect_key] = new_vals
                    else:
                        aspects.pop(aspect_key, None)
                    aspect_changed = True
                    results.append(f"Aspect {aspect_key} charging references removed")

            new_title = _scrub_charging_text(title)
            if new_title and new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title charging references removed: {title}")
            new_description = charging_token_re.sub(" ", description)
            new_description = re.sub(r"\s{2,}", " ", new_description)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description charging station references removed")
            # Always push buyer-visible offer listingDescription when charging cleanup runs
            # (inventory product.description alone is truncated / secondary for audit reads).
            if not description_changed and description:
                description_changed = True
                results.append("Offer listingDescription will be synced after charging cleanup")
            continue
        if key == "__hallucinated_leather__":
            if "Material" in aspects:
                aspects["Material"] = ["Faux Leather" if "leather" in str(m).lower() else m for m in aspects["Material"]]
                aspect_changed = True
            new_title = re.sub(r'\b(?:genuine|real)\s+leather\b', 'Faux Leather', title, flags=re.IGNORECASE)
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title Genuine Leather replaced with Faux Leather: {title}")
            new_description = re.sub(r'\b(?:genuine|real)\s+leather\b', 'Faux Leather', description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description Genuine Leather references changed to Faux Leather")
            continue
        if key == "__hallucinated_wood__":
            if "Material" in aspects:
                aspects["Material"] = ["MDF/Particle Board" if "wood" in str(m).lower() else m for m in aspects["Material"]]
                aspect_changed = True
            new_title = re.sub(r'\bsolid\s+wood\b', 'MDF / Particle Board', title, flags=re.IGNORECASE)
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title Solid Wood replaced with MDF: {title}")
            new_description = re.sub(r'\bsolid\s+wood\b', 'MDF/Particle Board', description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description Solid Wood references changed to MDF/Particle Board")
            continue
        if key == "__hallucinated_wood_species__":
            replacement = str(value or "").strip() or "Wood"
            replacement = re.sub(r"\s+", " ", replacement).strip()
            for aspect_key in ("Material", "Frame Material"):
                aspect_values = aspects.get(aspect_key)
                if not aspect_values:
                    continue
                if not isinstance(aspect_values, list):
                    aspect_values = [aspect_values]
                if any(
                    any(re.search(pattern, str(item), flags=re.IGNORECASE) for pattern in patterns)
                    for item in aspect_values
                    for patterns in WOOD_SPECIES_PATTERNS.values()
                ):
                    aspects[aspect_key] = [replacement]
                    aspect_changed = True
            species_pattern = r"\b(?:acacia|teak|oak|pine|rubberwood|walnut|bamboo|eucalyptus)\b(?:\s+wood)?"
            new_title = re.sub(species_pattern, replacement, title, flags=re.IGNORECASE)
            new_title = re.sub(r"\s+", " ", new_title).strip(" ,-/")
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title unsupported wood species replaced with source material: {title}")
            new_description = re.sub(species_pattern, replacement, description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append(f"Description unsupported wood species replaced with source material: {replacement}")
            continue
        if key == "__hallucinated_cushion__":
            if "Features" in aspects:
                aspects["Features"] = [
                    f for f in aspects["Features"]
                    if not any(re.search(pattern, str(f), flags=re.IGNORECASE) for pattern in CUSHION_PATTERNS)
                ]
                aspect_changed = True
            for aspect_key in ("Upholstery Material", "Cushion Color", "Fill Material"):
                if aspect_key in aspects:
                    aspects.pop(aspect_key, None)
                    aspect_changed = True
            new_title = re.sub(r"\bwith\s+cushions?\b", "", title, flags=re.IGNORECASE)
            new_title = re.sub(r"\bwith\s+cushion\b", "", new_title, flags=re.IGNORECASE)
            new_title = re.sub(r"\bcushions?\b", "", new_title, flags=re.IGNORECASE)
            new_title = re.sub(r"\s+", " ", new_title).strip(" ,-/")
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title cushion references removed: {title}")
            new_description = _remove_html_blocks_containing(description, CUSHION_PATTERNS)
            new_description = re.sub(r"\bwith\s+cushions?\b", "", new_description, flags=re.IGNORECASE)
            new_description = re.sub(r"\bcushions?\b", "", new_description, flags=re.IGNORECASE)
            new_description = re.sub(r"\s+", " ", new_description)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description cushion/upholstery references removed")
            continue
        if key == "__hallucinated_weather_resistance__":
            if "Features" in aspects:
                aspects["Features"] = [
                    f for f in aspects["Features"]
                    if not any(re.search(pattern, str(f), flags=re.IGNORECASE) for pattern in WEATHER_RESISTANCE_PATTERNS)
                ]
                aspect_changed = True
            new_description = _remove_html_blocks_containing(description, WEATHER_RESISTANCE_PATTERNS)
            for pattern in WEATHER_RESISTANCE_PATTERNS:
                new_description = re.sub(pattern, "", new_description, flags=re.IGNORECASE)
            new_description = re.sub(r"\s+", " ", new_description)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description unsupported weather-resistance references removed")
            continue
        if key == "__hallucinated_position_count__":
            combined_pattern = (
                r"\b(?:[2-9]|10|two|three|four|five|six|seven|eight|nine|ten)"
                r"[-\s]+(?:position|level)(?:\s+(?:backrest|recline|reclining|adjustment))?"
            )
            new_title = re.sub(combined_pattern, "Adjustable", title, flags=re.IGNORECASE)
            new_title = re.sub(r"\s+", " ", new_title).strip(" ,-/")
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title unsupported position count replaced with Adjustable: {title}")
            new_description = re.sub(combined_pattern, "Adjustable", description, flags=re.IGNORECASE)
            new_description = re.sub(r"Adjustable\s+backrest\s+lets", "Adjustable backrest lets", new_description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description unsupported position-count detail generalized to Adjustable")
            continue
        if key == "__hallucinated_foldable__":
            if "Features" in aspects:
                aspects["Features"] = [
                    f for f in aspects["Features"]
                    if not any(token in str(f).lower() for token in ["fold", "collaps"])
                ]
                aspect_changed = True
            new_title = re.sub(r'\b(?:foldable|collapsible|foldaway|folding|fold)\b\s*', '', title, flags=re.IGNORECASE)
            new_title = re.sub(r'\s+', ' ', new_title).strip()
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title foldable references removed: {title}")
            new_description = re.sub(r'Foldaway Design:', 'Compact Design:', description, flags=re.IGNORECASE)
            new_description = re.sub(r'\b(?:foldable|collapsible|foldaway|folding|fold)\b', '', new_description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description Foldable/Foldaway references removed")
            continue
        if key == "__hallucinated_audio__":
            if "Features" in aspects:
                aspects["Features"] = [f for f in aspects["Features"] if not any(w in str(f).lower() for w in ["bluetooth", "speaker", "audio"])]
                aspect_changed = True
            new_title = re.sub(r'\b(?:bluetooth|speaker|audio)\b\s*', '', title, flags=re.IGNORECASE)
            new_title = re.sub(r'\s+', ' ', new_title).strip()
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title audio references removed: {title}")
            new_description = re.sub(r'\b(?:bluetooth|speaker|audio|sound\s+system)\b', '', description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description Bluetooth/Audio references removed")
            continue
        if key == "__hallucinated_aluminum_rod__":
            if "Material" in aspects:
                aspects["Material"] = ["Iron" if any(w in str(m).lower() for w in ["aluminum", "aluminium"]) else m for m in aspects["Material"]]
                aspect_changed = True
            new_title = re.sub(r'\b(?:aluminum|aluminium)\b', 'Iron', title, flags=re.IGNORECASE)
            new_title = re.sub(r'\s+', ' ', new_title).strip()
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title pull rod material corrected to Iron: {title}")
            new_description = re.sub(r'\b(?:aluminum|aluminium)\s+(?:pull\s+)?rod\b', 'iron pull rod', description, flags=re.IGNORECASE)
            new_description = re.sub(r'\b(?:aluminum|aluminium)\s+telescoping\b', 'iron telescoping', new_description, flags=re.IGNORECASE)
            new_description = re.sub(r'\b(?:aluminum|aluminium)\s+handle\b', 'iron handle', new_description, flags=re.IGNORECASE)
            new_description = re.sub(r'\b(?:aluminum|aluminium)\b', 'iron', new_description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description pull rod material corrected to Iron")
            continue
        if key == "__hallucinated_tempered_glass__":
            if "Features" in aspects:
                aspects["Features"] = [f for f in aspects["Features"] if "tempered" not in str(f).lower()]
                aspect_changed = True
            new_title = re.sub(r'\btempered\s+glass\b', 'Glass', title, flags=re.IGNORECASE)
            new_title = re.sub(r'\s+', ' ', new_title).strip()
            if new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title tempered glass corrected to Glass: {title}")
            new_description = re.sub(r'\btempered\s+glass\b', 'glass', description, flags=re.IGNORECASE)
            if new_description != description:
                description = new_description
                description_changed = True
                results.append("Description tempered glass corrected to Glass")
            continue
        if key == "__incomplete_title__":
            new_title, _ = normalize_listing_title_for_ebay(title, source_title=source_title or "")
            if new_title and new_title != title:
                title = new_title
                title_changed = True
                results.append(f"Title truncated end word/character cleaned: {title}")
            continue
        if key == "__missing_foldable__":
            features = aspects.get("Features", [])
            if not isinstance(features, list):
                features = [features] if features else []
            features = [str(f).strip() for f in features if str(f).strip()]
            if "Foldable" not in features and "Collapsible" not in features:
                features.append("Foldable")
                aspects["Features"] = features
                aspect_changed = True
                results.append("Restored missing Foldable feature to Aspects")
            continue
        if key == "__truncated_html_cleanup__":
            # 1. Remove the truncation warning notice block
            description = re.sub(
                r'<div\b[^>]*>\s*<p\b[^>]*>\s*(?:<strong>)?📋 Complete Details:.*?</p>\s*</div>',
                '',
                description,
                flags=re.IGNORECASE | re.DOTALL
            )
            # 2. Split the description and drop the old truncated specifications part
            parts = re.split(r'<!--\s*Specifications Table\s*-->|<h3\b[^>]*>\s*SPECIFICATIONS\s*</h3>', description, flags=re.IGNORECASE)
            if len(parts) >= 2:
                description = parts[0].strip()
                # 3. Rebuild a 100% complete Specifications Table HTML
                assembly_required = infer_source_assembly_required(attrs, source_specs, source_description)
                new_table = rebuild_specifications_table(attrs, source_specs, aspects, assembly_required)
                description = replace_specifications_table_html(description, new_table)
                results.append("Description HTML specifications table rebuilt from scratch")
                description_changed = True
            else:
                # Fallback: update Material row only
                material_val = ""
                if "Material" in aspects:
                    material_val = aspects["Material"][0] if isinstance(aspects["Material"], list) else str(aspects["Material"])
                if not material_val and "Material" in attrs:
                    material_val = str(attrs["Material"])
                if material_val:
                    pat = r'(<td\b[^>]*>Material</td>\s*<td\b[^>]*>)[^<]*(</td>)?'
                    def _repl_mat(m):
                        td_start = m.group(1)
                        td_end = m.group(2) or "</td>"
                        return f"{td_start}{material_val}{td_end}"
                    description = re.sub(pat, _repl_mat, description, flags=re.IGNORECASE | re.DOTALL)
                results.append("Description Material cell updated")
                description_changed = True
            continue

        if key.startswith("__remove__"):
            real_key = key.replace("__remove__", "")
            if real_key in aspects:
                del aspects[real_key]
                aspect_changed = True
                results.append(f"Removed non-applicable aspect: {real_key}")
            continue
        # Normal aspect update
        aspects[key] = value
        aspect_changed = True
        results.append(f"Fixed {key}: {value}")

    dims = extract_all_dimensions(attrs)
    has_rebuildable_dimensions = bool(dims.get("length") and dims.get("width") and dims.get("height"))
    if description_changed and has_rebuildable_dimensions:
        assembly_required = first_aspect_text(aspects, "Assembly Required") or infer_source_assembly_required(
            attrs,
            source_specs,
            source_description,
        )
        rebuilt_table = rebuild_specifications_table(
            attrs,
            source_specs,
            aspects,
            assembly_required=assembly_required or "No",
        )
        if not _description_has_substantive_copy(description):
            fallback_base = source_description or description
            description = replace_specifications_table_html(fallback_base, rebuilt_table)
            results.append("Description restored from source copy after removing unsupported claims")
        elif "specifications" not in description.lower():
            description = replace_specifications_table_html(description, rebuilt_table)
            results.append("Description specifications table rebuilt from source dimensions/weight")

    if description:
        description = sanitize_generated_description_html(description)

    title, normalized_title_changed = normalize_listing_title_for_ebay(title, source_title=source_title or "")
    if normalized_title_changed:
        title_changed = True
        if not any("title" in item.lower() for item in results):
            results.append(f"Normalized title for eBay length safety: {title}")

    if (
        not aspect_changed
        and not category_changed
        and not description_changed
        and not title_changed
        and compatibility_payload is None
        and not new_category_name
        and not image_restore_requested
        and not video_changed
        and not video_removed
    ):
        return results

    try:
        matcher = get_category_matcher()
        offer_description = description
        # Read the live offer first so required-aspect completion uses the real
        # eBay category instead of a stale local category snapshot.
        if category_changed or aspect_changed or description_changed or title_changed or image_restore_requested or video_changed or video_removed:
            offers = ebay_client.get_offers_by_sku(sku)
            live_offer = _select_best_offer(offers, expected_listing_id=product_row["listing_id"])
            if live_offer:
                live_offer_category = (
                    live_offer.get("categoryId")
                    or (live_offer.get("category") or {}).get("categoryId")
                )

        # Step 0: Fill missing required aspects for the category (prevent eBay rejections)
        effective_category = new_category or live_offer_category or opt.get("categoryId", "")
        aspects = _fill_missing_required_aspects(aspects, effective_category, title)
        required_aspects, _ = matcher._get_category_aspects(str(effective_category)) if effective_category else ([], [])
        required_aspect_names = [aspect.get("name") for aspect in required_aspects if aspect.get("name")]

        # Step 1: Update inventory item only when product fields changed.
        needs_inventory_update = aspect_changed or description_changed or title_changed or image_restore_requested
        if needs_inventory_update:
            compressed = compress_html(description)
            if len(compressed) <= 4000:
                inventory_description = compressed
            else:
                inventory_description = smart_truncate_html(description, max_length=4000, min_length=3600)
            product_only_update = not category_changed and compatibility_payload is None and not image_restore_requested
            if product_only_update:
                _, _, aspects = _put_inventory_product_only(ebay_client, sku, title, inventory_description, aspects)
                results.append("Inventory product fields updated on eBay")
            else:
                from src.utils.dimension_helpers import build_package_weight_and_size
                specs = parse_json(product_row["specs"]) if product_row["specs"] else {}
                pws = build_package_weight_and_size(attrs, specs)

                db_images = parse_image_list(product_row["images"])

                # CRITICAL: never overwrite live imageUrls with raw GigaB2B signed
                # URLs. eBay rejects most signed URLs at fetch time and the live
                # listing collapses to whichever single image survived. Always
                # prefer the imageUrls eBay already hosts; only re-upload to EPS
                # when the live count is significantly worse than what we have
                # locally (e.g. previously corrupted by an old run of this script).
                live_inventory = ebay_client.get_inventory_item(sku) or {}
                live_image_urls = (live_inventory.get("product") or {}).get("imageUrls") or []
                expected_count = min(len(db_images), 24)
                if live_image_urls and len(live_image_urls) >= max(1, expected_count - 1):
                    image_urls_for_put = list(live_image_urls)
                elif db_images:
                    logging.warning(
                        f"[AUDIT] {sku}: live images degraded ({len(live_image_urls)} vs {expected_count}) — re-uploading to EPS"
                    )
                    image_urls_for_put = ebay_client.upload_images_to_eps(db_images, max_images=24)
                    if not image_urls_for_put:
                        image_urls_for_put = list(live_image_urls)  # last-resort fallback
                else:
                    image_urls_for_put = list(live_image_urls)

                live_quantity = (
                    ((live_inventory.get("availability") or {}).get("shipToLocationAvailability") or {})
                    .get("quantity")
                )
                from src.utils.ebay_quantity import normalize_ebay_listing_quantity

                inv_product = {
                    "title": title,
                    "description": inventory_description,
                    "image_urls": image_urls_for_put,
                    "price": product_row["suggested_price"] or product_row["price"] or 99.99,
                    "quantity": normalize_ebay_listing_quantity(live_quantity or 1),
                    "condition": "NEW",
                    "aspects": aspects,
                    "required_aspect_names": required_aspect_names,
                }
                if pws:
                    inv_product["packageWeightAndSize"] = pws

                ebay_client.create_or_replace_inventory_item(sku=sku, product=inv_product)
                results.append("Inventory item updated on eBay")

        if compatibility_payload is not None:
            compatible_products = compatibility_payload.get("compatibleProducts", [])
            if compatible_products:
                ebay_client.create_or_replace_product_compatibility(sku, compatible_products)
                results.append(f"Structured compatibility updated on eBay ({len(compatible_products)} rows)")
            else:
                ebay_client.delete_product_compatibility(sku)
                results.append("Structured compatibility cleared on eBay")

        # Step 2: Update offer listing/category when the live listing copy must change.
        offer_needs_update = category_changed or description_changed or ((needs_inventory_update or image_restore_requested) and not product_only_update)
        if offer_needs_update:
            if live_offer:
                offer_id = live_offer.get("offerId")
                if offer_id:
                    cat = new_category or live_offer_category or parse_json(product_row["optimization"]).get("categoryId")
                    offer_updated = ebay_client.update_offer_category(
                        offer_id, cat,
                        listing_description=offer_description
                    )
                    if offer_updated:
                        results.append(f"Offer {offer_id} updated (category={cat})")
                        try:
                            pub_res = ebay_client.publish_offer(offer_id)
                            if pub_res and pub_res.get("listingId"):
                                republished_listing_id = pub_res.get("listingId")
                                results.append(f"Offer {offer_id} republished to live listing ({pub_res.get('listingId')})")
                            else:
                                results.append(f"WARNING: Offer {offer_id} publish call did not return listingId")
                        except Exception as pub_err:
                            results.append(f"ERROR: Failed to republish offer {offer_id}: {pub_err}")
                    else:
                        results.append(f"ERROR: Offer {offer_id} update failed (category={cat})")
                else:
                    results.append("ERROR: No offerId found for live offer")
            else:
                results.append("ERROR: No live offer found for SKU")

        if product_only_update and (title_changed or aspect_changed) and live_offer and live_offer.get("offerId"):
            offer_id = live_offer["offerId"]
            publish_result = ebay_client.publish_offer(offer_id)
            listing_id = publish_result.get("listingId") if publish_result else None
            if listing_id:
                republished_listing_id = listing_id
                results.append(f"Offer {offer_id} republished after inventory product update")
            else:
                results.append(f"ERROR: Offer {offer_id} republish failed after inventory product update")

        if image_restore_requested and live_offer and live_offer.get("offerId"):
            offer_id = live_offer["offerId"]
            publish_result = ebay_client.publish_offer(offer_id)
            listing_id = publish_result.get("listingId") if publish_result else None
            if listing_id:
                republished_listing_id = listing_id
                results.append(f"Offer {offer_id} republished after image restore")
            else:
                results.append(f"ERROR: Offer {offer_id} republish failed after image restore")

        if video_changed:
            if live_offer and live_offer.get("offerId"):
                offer_id = live_offer["offerId"]
                publish_result = ebay_client.publish_offer(offer_id)
                listing_id = publish_result.get("listingId") if publish_result else None
                if listing_id:
                    republished_listing_id = listing_id
                    results.append(f"Offer {offer_id} republished after video sync")
                else:
                    results.append(f"ERROR: Offer {offer_id} republish failed after video sync")
            else:
                results.append("ERROR: No live offer found for video sync")

        if video_removed:
            if live_offer and live_offer.get("offerId"):
                offer_id = live_offer["offerId"]
                publish_result = ebay_client.publish_offer(offer_id)
                listing_id = publish_result.get("listingId") if publish_result else None
                if listing_id:
                    republished_listing_id = listing_id
                    results.append(f"Offer {offer_id} republished after video removal")
                else:
                    results.append(f"ERROR: Offer {offer_id} republish failed after video removal")
            else:
                results.append("ERROR: No live offer found for video removal")

        
        opt["aspects"] = aspects
        if new_category:
            opt["categoryId"] = new_category
        if new_category_name:
            opt["categoryName"] = new_category_name
        if title_changed:
            opt["title"] = title
        if description and description_changed:
            opt["description"] = description
        if compatibility_payload is not None:
            opt["motorsCompatibility"] = compatibility_payload
        if synced_video_id:
            opt["video_id"] = synced_video_id
            # A reused Media API ID was independently known to be LIVE before
            # attachment. Do not downgrade that fact to the ambiguous local
            # label "UPLOADED", which makes later recovery queues unreliable.
            opt["video_status"] = "LIVE" if reused_existing_live_video else "UPLOADED"
            opt["videoIds"] = [synced_video_id]
        if video_removed:
            opt.pop("video_id", None)
            opt.pop("video_status", None)
            opt.pop("videoIds", None)
            opt["source_video_status"] = "REMOVED_FROM_GIGA"
        columns = _table_columns(db_conn, "collected_products")
        assignments = ["optimization = ?"]
        params = [json.dumps(opt, ensure_ascii=False)]
        if republished_listing_id and "listing_id" in columns:
            assignments.append("listing_id = ?")
            params.append(republished_listing_id)
        if republished_listing_id and "status" in columns:
            assignments.append("status = ?")
            params.append("PUBLISHED")
        if "updated_at" in columns:
            assignments.append("updated_at = ?")
            params.append(datetime.now().isoformat(sep=" ", timespec="seconds"))
        params.append(sku)
        db_conn.execute(
            f"UPDATE collected_products SET {', '.join(assignments)} WHERE sku = ?",
            params,
        )
        db_conn.commit()
        results.append("Local DB updated")

    except Exception as e:
        results.append(f"ERROR [{sku}]: {e}")

    return results


def main(argv=None):
    parser = create_parser()
    args = parser.parse_args(argv)
    if args.sku and args.sku_file:
        parser.error("--sku and --sku-file cannot be used together")
    validate_fix_scope(args, parser)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path(args.report) if args.report else LOG_DIR / f"listing_audit_fix_{timestamp}.json"
    report_source = get_report_source(args)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Query published products. A report scope only supplies candidate SKUs;
    # every candidate is re-read from live eBay below before a fix is applied.
    if args.sku:
        rows = conn.execute(
            "SELECT sku, title, attributes, specs, optimization, description, images, videos, price, suggested_price, status, listing_id "
            "FROM collected_products WHERE sku = ?",
            (args.sku,)
        ).fetchall()
    elif args.sku_file or args.source_report:
        if args.sku_file:
            sku_list = _load_skus_from_file(args.sku_file)
        else:
            try:
                sku_list = _load_skus_from_audit_report(
                    args.source_report,
                    set(args.issue_types or []),
                )
            except ValueError as exc:
                parser.error(str(exc))
            if args.local_video_statuses:
                sku_list = filter_skus_by_local_video_status(
                    conn,
                    sku_list,
                    set(args.local_video_statuses),
                )
        if not sku_list:
            scope_label = args.sku_file or args.source_report
            print(f"⚠️ Selected SKU scope is empty: {scope_label}")
            conn.close()
            return 0
        placeholders = ",".join("?" for _ in sku_list)
        rows = conn.execute(
            f"SELECT sku, title, attributes, specs, optimization, description, images, videos, price, suggested_price, status, listing_id "
            f"FROM collected_products WHERE sku IN ({placeholders}) ORDER BY updated_at DESC",
            tuple(sku_list),
        ).fetchall()
    else:
        query = (
            "SELECT sku, title, attributes, specs, optimization, description, images, videos, price, suggested_price, status, listing_id "
            "FROM collected_products WHERE status = 'PUBLISHED' ORDER BY updated_at DESC"
        )
        params = []
        if args.limit is not None:
            query += " LIMIT ?"
            params.append(args.limit)
        rows = conn.execute(query, tuple(params)).fetchall()

    print(f"{'='*60}")
    print(f"  Listing Audit & Fix — {len(rows)} published listings")
    print(f"  Mode: {'FIX (will update eBay)' if args.fix else 'AUDIT ONLY (dry run)'}")
    print(f"  Source: {'LIVE EBAY vs GIGA' if args.live else 'LOCAL DB optimization vs GIGA'}")
    print(f"{'='*60}\n")

    # Initialize eBay client when fixing or reading live listing data.
    ebay_client = None
    if args.fix or args.live:
        try:
            from src.clients.real_ebay_client import create_real_ebay_client
            environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
            ebay_client = create_real_ebay_client(environment)
            if not ebay_client.oauth.is_authorized():
                print("❌ eBay not authorized! Run: python tools/refresh_token.py")
                sys.exit(1)
            print("✅ eBay client authorized\n")
        except Exception as e:
            print(f"❌ Failed to initialize eBay client: {e}")
            sys.exit(1)

    all_issues = []
    all_transport_issues = []
    fixed_count = 0
    error_count = 0
    skipped_clean_frozen = 0
    clean_state_recorded = 0
    transport_issue_count = 0
    severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}

    for row in rows:
        sku = row["sku"]
        title = row["title"] or ""
        source_opt = parse_json(row["optimization"])
        source_fingerprint = build_audit_fingerprint(
            build_source_audit_payload(
                title,
                row["description"] or "",
                row["attributes"],
                row["specs"],
                images_raw=row["images"],
                videos_raw=row["videos"],
            )
        )
        opt_title = ""
        try:
            opt_title = source_opt.get("title", "")
        except:
            pass

        audit_opt_raw = row["optimization"]
        preflight_issues = []
        live_inventory = None
        live_offer = None
        current_listing_id = row["listing_id"]
        if args.live and ebay_client:
            try:
                live_inventory, live_offer = _fetch_live_listing_context(
                    ebay_client,
                    sku,
                    expected_listing_id=row["listing_id"],
                )
                if not live_inventory:
                    preflight_issues.append({
                        "type": "live_inventory_missing",
                        "severity": "HIGH",
                        "detail": "Could not read live eBay inventory item for this SKU",
                    })
                if not live_offer:
                    preflight_issues.append({
                        "type": "live_offer_missing",
                        "severity": "HIGH",
                        "detail": "Could not read live eBay offer/listingDescription for this SKU",
                    })
                current_listing_id = (
                    ((live_offer or {}).get("listing") or {}).get("listingId")
                    or (live_offer or {}).get("listingId")
                    or row["listing_id"]
                )
                live_opt = build_live_listing_opt_snapshot(
                    source_opt,
                    inventory_item=live_inventory,
                    offer=live_offer,
                )
                audit_opt_raw = json.dumps(live_opt, ensure_ascii=False)
            except Exception as e:
                preflight_issues.append({
                    "type": "live_fetch_failed",
                    "severity": "HIGH",
                    "detail": f"Failed to fetch live eBay listing data: {e}",
                })

        live_fingerprint = None
        if args.live:
            live_fingerprint = build_audit_fingerprint(
                build_live_audit_payload(
                    audit_opt_raw,
                    listing_id=current_listing_id,
                    live_inventory=live_inventory,
                )
            )
            if (
                not preflight_issues
                and not args.ignore_clean_freeze
                and is_listing_frozen_clean(
                    source_opt,
                    source_fingerprint=source_fingerprint,
                    live_fingerprint=live_fingerprint,
                    listing_id=current_listing_id,
                )
            ):
                skipped_clean_frozen += 1
                time.sleep(0.2)
                continue

        issues, fixes = audit_single_product(
            sku,
            title,
            row["attributes"],
            row["specs"],
            audit_opt_raw,
            row["description"],
            ebay_client=ebay_client if (args.fix or args.live) else None,
            images_raw=row["images"],
            videos_raw=row["videos"],
            live_inventory=live_inventory if args.live else None,
        )
        issues = preflight_issues + issues
        transport_issues, content_issues = split_transport_issues(issues)
        issue_types = sorted(
            {
                str(issue.get("type", "")).strip()
                for issue in issues
                if str(issue.get("type", "")).strip()
            }
        )

        if not issues and not fixes:
            if args.live and args.record_clean_state:
                if persist_listing_audit_state(
                    conn,
                    sku,
                    state="clean",
                    source_fingerprint=source_fingerprint,
                    live_fingerprint=live_fingerprint,
                    listing_id=current_listing_id,
                ):
                    clean_state_recorded += 1
            if args.live:
                time.sleep(0.2)
            continue

        print(f"🔍 {sku}: {opt_title[:60] or title[:60]}")
        for issue in content_issues:
            sev = issue.get("severity", "MEDIUM")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "⚪")
            print(f"   {icon} [{sev}] {issue['detail']}")
        for issue in transport_issues:
            sev = issue.get("severity", "MEDIUM")
            icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}.get(sev, "⚪")
            print(f"   {icon} [TRANSPORT/{sev}] {issue['detail']}")

        fix_results = []
        selected_fixes = filter_fixes_by_key(fixes, set(args.fix_keys or []))
        if args.fix and selected_fixes and ebay_client:
            print(f"   🔧 Applying {len(selected_fixes)} selected fixes...")
            fix_results = fix_listing_on_ebay(
                sku,
                dict(row),
                selected_fixes,
                ebay_client,
                conn,
                base_opt_raw=audit_opt_raw if args.live else None,
            )
            for r in fix_results:
                if "ERROR" in r:
                    print(f"   ❌ {r}")
                    error_count += 1
                else:
                    print(f"   ✅ {r}")
            if fix_results and all("ERROR" not in r for r in fix_results):
                fixed_count += 1
            # Rate limit
            time.sleep(1)

        if args.live:
            if args.fix:
                fix_succeeded = bool(fix_results) and all("ERROR" not in r for r in fix_results)
                persist_listing_audit_state(
                    conn,
                    sku,
                    state="pending_verify" if fix_succeeded else "dirty",
                    source_fingerprint=source_fingerprint,
                    live_fingerprint=live_fingerprint,
                    listing_id=current_listing_id,
                    issue_types=issue_types,
                )
            else:
                persist_listing_audit_state(
                    conn,
                    sku,
                    state="dirty",
                    source_fingerprint=source_fingerprint,
                    live_fingerprint=live_fingerprint,
                    listing_id=current_listing_id,
                    issue_types=issue_types,
                )

        base_item = {
            "sku": sku,
            "title": (opt_title or title)[:120],
            "listing_id": current_listing_id,
        }
        if content_issues:
            all_issues.append({
                **base_item,
                "issues": content_issues,
                "fix_keys": sorted(fixes.keys()),
                "selected_fix_keys": sorted(selected_fixes.keys()) if args.fix else [],
                "fixes_applied": fix_results if args.fix else [],
            })
        if transport_issues:
            transport_issue_count += 1
            all_transport_issues.append({
                **base_item,
                "issues": transport_issues,
                "fix_keys": [],
                "fixes_applied": [],
            })
        print()
        if args.live:
            time.sleep(0.2)

    # Summary
    print(f"\n{'='*60}")
    print(f"  AUDIT SUMMARY")
    print(f"{'='*60}")
    print(f"  Total published: {len(rows)}")
    print(f"  Listings with content issues: {len(all_issues)}")
    if args.live:
        print(f"  Listings with transport/availability issues: {transport_issue_count}")
    print(f"  🔴 CRITICAL: {severity_counts.get('CRITICAL', 0)}")
    print(f"  🟠 HIGH: {severity_counts.get('HIGH', 0)}")
    print(f"  🟡 MEDIUM: {severity_counts.get('MEDIUM', 0)}")
    if args.live:
        print(f"  🧊 Clean frozen skipped: {skipped_clean_frozen}")
        if args.record_clean_state:
            print(f"  📝 Clean state recorded: {clean_state_recorded}")
    if args.fix:
        print(f"  ✅ Fixed: {fixed_count}")
        print(f"  ❌ Errors: {error_count}")
    print(f"{'='*60}")

    # Save report
    report = {
        "generated_at": datetime.now().isoformat(),
        "mode": "fix" if args.fix else ("live_audit" if args.live else "audit"),
        "source": report_source,
        "total_published": len(rows),
        "total_with_issues": len(all_issues),
        "total_transport_failures": transport_issue_count if args.live else 0,
        "severity_counts": severity_counts,
        "skipped_clean_frozen": skipped_clean_frozen if args.live else 0,
        "clean_state_recorded": clean_state_recorded if args.live and args.record_clean_state else 0,
        "fixed_count": fixed_count if args.fix else 0,
        "issues": all_issues,
        "transport_issues": all_transport_issues if args.live else [],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 Full report: {report_path}")

    if args.email:
        try:
            if _send_audit_email(report, report_path):
                print("📧 Audit email sent")
            else:
                print("⚠️ Audit email sender returned false")
                conn.close()
                return 2
        except Exception as e:
            print(f"⚠️ Audit email failed: {e}")
            conn.close()
            return 2

    conn.close()
    if args.exit_zero_on_issues:
        return 0
    return len(all_issues) + transport_issue_count


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    sys.exit(0 if main() == 0 else 1)
