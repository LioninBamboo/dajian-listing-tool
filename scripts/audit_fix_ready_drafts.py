"""Audit and fix READY / READY_TO_PUBLISH draft quality issues."""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.clients.dajian_client import DaJianClient
from src.services.ebay_category_matcher import create_category_matcher
from src.services.ebay_publisher import EbayPublisher
from src.services.taxonomy_constants import INVALID_CATEGORY_REMAP, PROTECTED_STORED_CATEGORY_IDS
from src.utils.dimension_helpers import (
    extract_dajian_measurements,
    extract_all_dimensions,
    extract_product_dimensions_from_text,
    extract_product_weight_from_text,
    replace_description_measurements,
)
from src.utils.publish_autofix import sanitize_placeholder_aspects
from src.utils.report_images import normalize_image_list
from src.utils.publish_validation import first_aspect_text, measurement_issue
from src.utils.listing_quality_gate import (
    normalize_generated_listing,
)
from src.services.listing_qc import run_listing_qc
from src.utils.title_sanitizer import sanitize_listing_title

DB_PATH = ROOT / "ebay_collection.db"
LOG_DIR = ROOT / "logs"

# Manual measurement overrides for supplier SKUs whose source data omits assembled
# dimensions but the listing images provide clear overall size callouts.
MANUAL_MEASUREMENT_OVERRIDES = {
    "W331S00208": {
        "Assembled Length (in.)": "31.50",
        "Assembled Width (in.)": "15.75",
        "Assembled Height (in.)": "70.87",
    },
    "W1151S04808": {
        "Assembled Length (in.)": "71.00",
        "Assembled Width (in.)": "31.50",
        "Assembled Height (in.)": "30.00",
    },
    "W1677P247442": {
        "Assembled Length (in.)": "192.00",
        "Assembled Width (in.)": "155.00",
        "Assembled Height (in.)": "84.00",
    },
    "W2564S00135": {
        "Assembled Length (in.)": "114.57",
        "Assembled Width (in.)": "91.34",
        "Assembled Height (in.)": "34.65",
    },
    "W2564S00136": {
        "Assembled Length (in.)": "114.57",
        "Assembled Width (in.)": "91.34",
        "Assembled Height (in.)": "34.65",
    },
    "W2339P461256": {
        "Assembled Length (in.)": "44.5",
        "Assembled Width (in.)": "48.8",
        "Assembled Height (in.)": "35.4",
    },
    "W2339P461262": {
        "Assembled Length (in.)": "44.5",
        "Assembled Width (in.)": "48.8",
        "Assembled Height (in.)": "35.4",
    },
    "W2339P461271": {
        "Assembled Length (in.)": "44.5",
        "Assembled Width (in.)": "48.8",
        "Assembled Height (in.)": "35.4",
    },
    "W3098P470278": {
        "Assembled Length (in.)": "65.5",
        "Assembled Width (in.)": "20.5",
        "Assembled Height (in.)": "56.8",
        "Product Weight (lbs.)": "56.81",
    },
    "N704F201251A": {
        "Assembled Length (in.)": "89.4",
        "Assembled Width (in.)": "62.3",
        "Assembled Height (in.)": "31.9",
    },
    "N704G201258A": {
        "Assembled Length (in.)": "44.5",
        "Assembled Width (in.)": "44.5",
        "Assembled Height (in.)": "29.5",
    },
    "N704G201258L": {
        "Assembled Length (in.)": "44.5",
        "Assembled Width (in.)": "44.5",
        "Assembled Height (in.)": "29.5",
    },
    "N704P448453A": {
        "Assembled Length (in.)": "27.55",
        "Assembled Width (in.)": "26.77",
        "Assembled Height (in.)": "31.88",
    },
    "N704P448453E": {
        "Assembled Length (in.)": "27.55",
        "Assembled Width (in.)": "26.77",
        "Assembled Height (in.)": "31.88",
    },
    "N707S003007B": {
        "Assembled Length (in.)": "53.14",
        "Assembled Width (in.)": "29.52",
        "Assembled Height (in.)": "36.40",
    },
    "W1117S00330": {
        "Assembled Length (in.)": "105.0",
        "Assembled Width (in.)": "68.0",
        "Assembled Height (in.)": "25.0",
    },
    "W1117S00335": {
        "Assembled Length (in.)": "105.0",
        "Assembled Width (in.)": "68.0",
        "Assembled Height (in.)": "25.0",
    },
    "W1117S00385": {
        "Assembled Length (in.)": "105.0",
        "Assembled Width (in.)": "68.0",
        "Assembled Height (in.)": "25.0",
    },
    "W1117S00389": {
        "Assembled Length (in.)": "105.0",
        "Assembled Width (in.)": "68.0",
        "Assembled Height (in.)": "25.0",
    },
    "W1117S00414": {
        "Assembled Length (in.)": "105.0",
        "Assembled Width (in.)": "68.0",
        "Assembled Height (in.)": "25.0",
    },
    "W1117S00415": {
        "Assembled Length (in.)": "105.0",
        "Assembled Width (in.)": "68.0",
        "Assembled Height (in.)": "25.0",
    },
}

DIMENSION_IMAGE_NOTES = {
    "W331S00208": "Overall cabinet size is 31.5 in W x 15.75 in D x 70.87 in H based on the product dimension image.",
    "W1151S04808": "Table size is 71 in L x 31.5 in W x 30 in H. Chair and set layout details are shown in the product dimension image.",
    "W1677P247442": "Overall inflated size is 192 in L x 155 in W x 84 in H. Detailed play-area measurements are shown in the product dimension image.",
    "W2564S00135": "This sectional has a complex convertible layout. The listed overall footprint uses the expanded layout; detailed component measurements are shown in the product dimension image.",
    "W2564S00136": "This sectional has a complex convertible layout. The listed overall footprint uses the expanded layout; detailed component measurements are shown in the product dimension image.",
    "N704F201251A": "This outdoor patio set has an L-shaped layout. The listed overall footprint uses the product size image; detailed sofa and coffee table measurements are shown in the product dimension image.",
    "N704G201258A": "The dining table measures approximately 44.5 in diameter x 29.5 in H. Each chair measures approximately 22.4 in W x 21.5 in D x 28.7 in H based on the product dimension image.",
    "N704G201258L": "The dining table measures approximately 44.5 in diameter x 29.5 in H. Each chair measures approximately 22.4 in W x 21.5 in D x 28.7 in H based on the product dimension image.",
    "N704P448453A": "This 2-piece chair set uses the single-chair size shown in the product dimension image. Each chair measures approximately 27.55 in W x 26.77 in D x 31.88 in H.",
    "N704P448453E": "This 2-piece chair set uses the single-chair size shown in the product dimension image. Each chair measures approximately 27.55 in W x 26.77 in D x 31.88 in H.",
    "W1117S00330": "Overall sectional footprint is approximately 105 in L x 68 in W x 25 in H based on the supplier dimension image. Additional seat-depth callouts are shown in the product size image.",
    "W1117S00335": "Overall sectional footprint is approximately 105 in L x 68 in W x 25 in H based on the supplier dimension image. Additional seat-depth callouts are shown in the product size image.",
    "W1117S00385": "Overall sectional footprint is approximately 105 in L x 68 in W x 25 in H based on the supplier dimension image. Additional seat-depth callouts are shown in the product size image.",
    "W1117S00389": "Overall sectional footprint is approximately 105 in L x 68 in W x 25 in H based on the supplier dimension image. Additional seat-depth callouts are shown in the product size image.",
    "W1117S00414": "Overall sectional footprint is approximately 105 in L x 68 in W x 25 in H based on the supplier dimension image. Additional seat-depth callouts are shown in the product size image.",
    "W1117S00415": "Overall sectional footprint is approximately 105 in L x 68 in W x 25 in H based on the supplier dimension image. Additional seat-depth callouts are shown in the product size image.",
}

TARGETED_COPY_REPAIRS = {
    "B2765P523551": {
        "aspects_remove": ["Assembly Required"],
        "description_subs": [{
            "pattern": r'(?is)<tr[^>]*data-assembly-note="true"[^>]*>.*?</tr>',
            "repl": "",
            "note": "removed unsupported assembly row from trunk copy",
        }],
    },
    "W2887P511374": {
        "aspects_set": {"Type": "Rocking Chair"},
        "aspects_remove": ["Assembly Required"],
        "description_subs": [
            {
                "pattern": r'(?is)<tr[^>]*data-assembly-note="true"[^>]*>.*?</tr>',
                "repl": "",
                "note": "removed unsupported assembly row from rocker copy",
            },
            {
                "pattern": r"(?i)fits your body curve perfectly to make you sink in and release all muscle tension after long hours of work\.?",
                "repl": "provides full-body cushioned support for everyday seating.",
                "note": "removed unsupported muscle-tension benefit wording",
            },
            {
                "pattern": r"(?i)helps you calm down and melt away daily fatigue slowly\.?",
                "repl": "supports gentle rocking for everyday relaxation.",
                "note": "removed unsupported fatigue-reduction wording",
            },
        ],
    },
    "W2887P511377": {
        "aspects_set": {"Type": "Rocking Chair"},
        "aspects_remove": ["Assembly Required"],
        "description_subs": [
            {"pattern": r'(?is)<tr[^>]*data-assembly-note="true"[^>]*>.*?</tr>', "repl": "", "note": "removed unsupported assembly row from rocker copy"},
            {"pattern": r"(?i)fits your body curve perfectly to make you sink in and release all muscle tension after long hours of work\.?", "repl": "provides full-body cushioned support for everyday seating.", "note": "removed unsupported muscle-tension benefit wording"},
            {"pattern": r"(?i)helps you calm down and melt away daily fatigue slowly\.?", "repl": "supports gentle rocking for everyday relaxation.", "note": "removed unsupported fatigue-reduction wording"},
        ],
    },
    "W2887P511379": {
        "aspects_set": {"Type": "Rocking Chair"},
        "aspects_remove": ["Assembly Required"],
        "description_subs": [
            {"pattern": r'(?is)<tr[^>]*data-assembly-note="true"[^>]*>.*?</tr>', "repl": "", "note": "removed unsupported assembly row from rocker copy"},
            {"pattern": r"(?i)fits your body curve perfectly to make you sink in and release all muscle tension after long hours of work\.?", "repl": "provides full-body cushioned support for everyday seating.", "note": "removed unsupported muscle-tension benefit wording"},
            {"pattern": r"(?i)helps you calm down and melt away daily fatigue slowly\.?", "repl": "supports gentle rocking for everyday relaxation.", "note": "removed unsupported fatigue-reduction wording"},
        ],
    },
    "W2887P511381": {
        "aspects_set": {"Type": "Rocking Chair"},
        "aspects_remove": ["Assembly Required"],
        "description_subs": [
            {"pattern": r'(?is)<tr[^>]*data-assembly-note="true"[^>]*>.*?</tr>', "repl": "", "note": "removed unsupported assembly row from rocker copy"},
            {"pattern": r"(?i)fits your body curve perfectly to make you sink in and release all muscle tension after long hours of work\.?", "repl": "provides full-body cushioned support for everyday seating.", "note": "removed unsupported muscle-tension benefit wording"},
            {"pattern": r"(?i)helps you calm down and melt away daily fatigue slowly\.?", "repl": "supports gentle rocking for everyday relaxation.", "note": "removed unsupported fatigue-reduction wording"},
        ],
    },
    "W3835P484072": {
        "aspects_set": {"Upholstery Fabric": "Chenille", "Indoor/Outdoor": "Indoor"},
        "aspects_remove": ["Mounting", "Leg Style"],
    },
    "W5568P524450": {"aspects_set": {"Upholstery Fabric": "Fabric"}},
    "W5568P524830": {"aspects_set": {"Upholstery Fabric": "Fabric"}},
    "W5568P524834": {"aspects_set": {"Upholstery Fabric": "Fabric"}},
    "W5368P517797": {"aspects_set": {"Type": "Ottoman"}},
    "W5568P506758": {
        "aspects_set": {"Type": "Armchair", "Indoor/Outdoor": "Indoor"},
    },
    "W5368P503714": {
        "features_remove": ["Foldable"],
        "aspects_remove": ["Mounting"],
        "description_subs": [{
            "pattern": r"(?i)will not collapse after long-term sitting",
            "repl": "is designed to retain its shape during everyday seating",
            "note": "removed collapse wording that was misread as foldable",
        }],
    },
    "W5368P460480": {
        "title": "Set of 2 Modern Nightstands with 2 Drawers, Natural Wood Color, Bedroom Storage",
        "aspects_remove": ["Mounting Type"],
        "description_subs": [{
            "pattern": r"(?i)natural wood-tone bedside tables",
            "repl": "bedside tables in the supplier-listed Natural Wood color",
            "note": "clarified Natural Wood as a color rather than solid-wood material",
        }],
    },
    "W3393S00009": {
        "description_subs": [{
            "pattern": r"(?is)<li[^>]*>\s*Crafted from particle board.*?</li>",
            "repl": '<li style="margin-bottom:10px">Gaming and Dining Use: The removable top switches between dining and gaming configurations, with an included game mat.</li>',
            "note": "removed material sentence from supplier-conflicted game-table copy",
        }],
    },
    "N707S185531B": {
        "features_remove": ["Foldable"],
        "description_subs": [
            {
                "pattern": r"(?i)foldable\s+drop\s+leaf",
                "repl": "drop leaf",
                "note": "removed unsupported foldable wording from drop-leaf copy",
            },
        ],
    },
    "W2531P498306": {
        "features_remove": ["Foldable Storage"],
    },
    "W2531P498304": {
        "description_subs": [
            {
                "pattern": r"(?i)\s*-\s*no\s+foldable\s+mechanism\s+required\s+thanks\s+to\s+its\s+space-efficient\s+profile\.?",
                "repl": "",
                "note": "removed unsupported foldable sentence from bike description",
            },
        ],
    },
    "W5819S00010": {
        "title": "King Size Spindle Four Poster Platform Bed Frame Curved Headboard Walnut",
        "features_keep": [
            "No Box Spring Needed",
            "Assembly Instructions",
            "Heavy Duty Metal Slat Support",
            "Squeak Resistant",
            "Under Bed Storage",
        ],
        "description_subs": [
            {
                "pattern": r"(?is)(<h2[^>]*>)King Size Platform Bed Frame with Charging Station LED Headboard Walnut Wood(</h2>)",
                "repl": r"\1King Size Spindle Four Poster Platform Bed Frame Curved Headboard Walnut\2",
                "note": "replaced unsupported charging/LED title block inside description",
            },
            {
                "pattern": r"(?is)<li[^>]*>\s*<strong>\s*Integrated Charging Station:.*?</li>",
                "repl": "",
                "note": "removed unsupported charging-station bullet from bed description",
            },
            {
                "pattern": r"(?is)<li[^>]*>\s*<strong>\s*LED Accent Lighting.*?</li>",
                "repl": "",
                "note": "removed unsupported LED-lighting bullet from bed description",
            },
            {
                "pattern": r"(?i)Ideal for tech-savvy homeowners seeking retro-modern aesthetics with practical upgrades like charging and ambient lighting - no extra furniture or power strips needed\.",
                "repl": "",
                "note": "removed unsupported charging/lighting lifestyle sentence",
            },
        ],
    },
}

READY_PROTECTED_CATEGORY_IDS = PROTECTED_STORED_CATEGORY_IDS
REQUIRED_MEASUREMENT_ASPECT_KEYS = ("Item Length", "Item Width", "Item Height")
READY_LIKE_STATUSES = ("READY", "READY_TO_PUBLISH")


def infer_known_layout_measurements(sku: str, title: str):
    """Return trusted dimensions from product size images for known supplier layouts."""
    sku = (sku or "").strip().upper()
    text = (title or "").lower()
    if sku == "W714S00449":
        return {
            "Assembled Length (in.)": "40.9",
            "Assembled Width (in.)": "34.2",
            "Assembled Height (in.)": "33.0",
        }

    if not sku.startswith("W714S"):
        return {}

    is_modular_sofa = any(
        marker in text
        for marker in (
            "modular sectional",
            "sectional sofa",
            "u-shaped modular",
            "u shaped modular",
            "modern l -",
            "modern u -",
            "corduroy +",
        )
    )
    if not is_modular_sofa:
        return {}

    length = width = height = None
    if sku == "W714S01590":
        length, width, height = 151.1, 116.1, 27.1
    # W714S01783-W714S01794 include explicit product-size images.
    elif "017" in sku:
        if "oversized 3" in text:
            length, width = 116.9, 40.9
        elif "oversized 4" in text:
            length, width = 151.9, 40.9
        elif re.search(r"\b3[- ]?seat|\b3\s*-|\b3\s+seat", text):
            length, width = 116.9, 69.7
        elif re.search(r"\b4[- ]?seat|\b4\s*-|\b4\s+seat", text):
            length, width = 151.9, 69.7
        height = 27.1
    # W714S01576-W714S01590 are corner/U modular sets with product-size images.
    elif "015" in sku and not re.search(r'\b(?:82|117|153)\s*"', text):
        if re.search(r"\bwith\s+4\b|\b4\s*-|\b4\s+seat", text):
            length, width = 116.1, 81.1
        elif re.search(r"\bwith\s+5\b|\b5\s*-|\b5\s+seat", text):
            length, width = 116.1, 116.1
        elif re.search(r"\bwith\s+6\b|\b6\s*-|\b6\s+seat", text):
            length, width = 151.1, 116.1
        height = 27.1
    # W714S01465-W714S01509 titles carry the overall layout length.
    else:
        title_size = re.search(r'\b(82|117|153)\s*"', text)
        if title_size:
            length = float(title_size.group(1))
            straight_layout = "oversized" in text and not any(marker in text for marker in ("modern l", "modern u", "l-shaped", "u-shaped"))
            width = 40.9 if straight_layout else 69.7
            height = 27.1

    if not (length and width and height):
        return {}
    return {
        "Assembled Length (in.)": f"{length:g}",
        "Assembled Width (in.)": f"{width:g}",
        "Assembled Height (in.)": f"{height:g}",
    }


def normalize_known_title(sku: str, title: str):
    text, _ = sanitize_listing_title((title or "").replace("Sofafor", "Sofa for"))
    if (sku or "").upper() == "W714S01590":
        text = re.sub(
            r"6\s*-\s*Camel\s*\+\s*Corduroy\s*\+\s*4\s*Seat",
            "6 - Camel + Corduroy + 6 Seat",
            text,
            flags=re.IGNORECASE,
        )
    return text


def apply_targeted_copy_repairs(sku: str, opt: dict):
    repairs = TARGETED_COPY_REPAIRS.get((sku or "").strip().upper())
    if not repairs:
        return []

    notes = []
    if repairs.get("title"):
        desired_title = repairs["title"].strip()
        current_title = (opt.get("title") or "").strip()
        if desired_title and current_title != desired_title:
            opt["title"] = desired_title
            notes.append(f"title -> {desired_title}")

    aspects = dict(opt.get("aspects") or {})
    for key in repairs.get("aspects_remove") or []:
        if key in aspects:
            aspects.pop(key, None)
            notes.append(f"removed aspect: {key}")
    for key, value in (repairs.get("aspects_set") or {}).items():
        desired = ensure_list_value(value)
        if aspects.get(key) != desired:
            aspects[key] = desired
            notes.append(f"{key} -> {', '.join(desired)}")
    features = ensure_list_value(aspects.get("Features"))
    if repairs.get("features_keep") is not None:
        desired_features = [str(value).strip() for value in repairs.get("features_keep") or [] if str(value).strip()]
        if features != desired_features:
            aspects["Features"] = desired_features
            notes.append(f"features -> {', '.join(desired_features)}")
    elif repairs.get("features_remove"):
        remove_set = {str(value).strip() for value in repairs.get("features_remove") or [] if str(value).strip()}
        removed = [value for value in features if value in remove_set]
        if removed:
            aspects["Features"] = [value for value in features if value not in remove_set]
            notes.append(f"removed features: {', '.join(removed)}")
    opt["aspects"] = aspects

    description = opt.get("description") or ""
    updated_description = description
    for rule in repairs.get("description_subs") or []:
        updated_description, count = re.subn(rule["pattern"], rule.get("repl", ""), updated_description)
        if count:
            notes.append(rule.get("note") or f"description replacement x{count}")
    if updated_description != description:
        updated_description = re.sub(r"\s{2,}", " ", updated_description).strip()
        opt["description"] = updated_description

    return notes


def build_dimension_image_note(sku: str, title: str, attrs: dict):
    static_note = DIMENSION_IMAGE_NOTES.get(sku)
    if static_note:
        return static_note

    inferred = infer_known_layout_measurements(sku, title)
    if not inferred:
        return None

    length = inferred.get("Assembled Length (in.)") or attrs.get("Assembled Length (in.)")
    width = inferred.get("Assembled Width (in.)") or attrs.get("Assembled Width (in.)")
    height = inferred.get("Assembled Height (in.)") or attrs.get("Assembled Height (in.)")
    if sku == "W714S00449":
        return (
            f"Overall chair size is approximately {length} in W x {width} in D x {height} in H "
            "based on the product dimension image."
        )
    return (
        f"Overall modular sectional footprint is approximately {length} in L x {width} in W x {height} in H "
        "for this selected layout. Because the sofa has a complex modular shape, please refer to the product "
        "dimension image for detailed module and layout measurements."
    )


def parse_json_object(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def parse_json_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return list(value)
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def ensure_list_value(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def normalize_category_id(category_id):
    category_text = str(category_id or "").strip()
    if not category_text:
        return None
    return INVALID_CATEGORY_REMAP.get(category_text, category_text)


@lru_cache(maxsize=1)
def get_dajian_client():
    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    if not client_id or not client_secret:
        return None
    return DaJianClient(client_id, client_secret)


@lru_cache(maxsize=512)
def fetch_dajian_detail(sku: str):
    client = get_dajian_client()
    if not client:
        return {}
    try:
        detail = client.get_product_detail_by_sku(sku)
    except Exception:
        return {}
    if not detail:
        return {}
    return detail


@lru_cache(maxsize=512)
def fetch_dajian_measurements(sku: str):
    detail = fetch_dajian_detail(sku)
    if not detail:
        return {}
    return extract_dajian_measurements(detail)


@lru_cache(maxsize=512)
def fetch_dajian_images(sku: str):
    detail = fetch_dajian_detail(sku)
    if not detail:
        return []
    return normalize_image_list(detail.get("imageUrls") or [], max_images=24)


def complete_aspects_for_category(matcher, category_id, title, aspects, category_name=None):
    required_aspects, recommended_aspects = matcher._get_category_aspects(str(category_id))
    return matcher._complete_aspects(
        aspects,
        required_aspects,
        recommended_aspects,
        title or "",
        category_name or f"Category {category_id}",
    )


def choose_ready_category(
    matcher,
    source_title,
    draft_title,
    description,
    aspects,
    current_category_id,
    current_category_name,
):
    candidates = []
    seen_titles = set()

    for label, candidate_title in (("source", source_title), ("draft", draft_title)):
        normalized_title = (candidate_title or "").strip()
        if not normalized_title or normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)

        candidate_id, candidate_name, completed_aspects = matcher.get_category_and_aspects(
            normalized_title,
            aspects,
            description or "",
        )
        candidate_id = normalize_category_id(candidate_id)
        candidate_id, candidate_name = matcher.canonicalize_category(
            normalized_title,
            candidate_id,
            candidate_name,
            description or "",
        )
        candidates.append({
            "label": label,
            "title": normalized_title,
            "category_id": candidate_id,
            "category_name": candidate_name,
            "aspects": completed_aspects or aspects,
            "plausible": bool(
                candidate_id and matcher.is_category_plausible_for_text(
                    normalized_title,
                    candidate_id,
                    candidate_name,
                )
            ),
            "strong": bool(candidate_id and candidate_id != "38208"),
        })

    current_id = normalize_category_id(current_category_id)
    title_context = " ".join(
        part.strip() for part in (source_title, draft_title) if part and part.strip()
    )
    current_id, current_category_name = matcher.canonicalize_category(
        title_context or source_title or draft_title,
        current_id,
        current_category_name,
        description or "",
    )
    preferred = next((candidate for candidate in candidates if candidate["plausible"] and candidate["strong"]), None)
    if not preferred:
        preferred = next((candidate for candidate in candidates if candidate["plausible"]), None)
    if not preferred and candidates:
        preferred = candidates[0]

    current_plausible_by_text = bool(
        current_id and matcher.is_category_plausible_for_text(
            title_context or source_title or draft_title,
            current_id,
            current_category_name,
        )
    )
    current_plausible = bool(
        current_id and (
            current_plausible_by_text
            or (current_id in READY_PROTECTED_CATEGORY_IDS and not preferred)
        )
    )

    if current_id and current_plausible:
        return (
            current_id,
            current_category_name or (preferred["category_name"] if preferred else None),
            complete_aspects_for_category(
                matcher,
                current_id,
                source_title or draft_title,
                aspects,
                current_category_name,
            ),
        )

    if preferred and preferred["category_id"] and preferred["plausible"]:
        return preferred["category_id"], preferred["category_name"], preferred["aspects"]

    if current_id:
        return (
            current_id,
            current_category_name,
            complete_aspects_for_category(
                matcher,
                current_id,
                source_title or draft_title,
                aspects,
                current_category_name,
            ),
        )

    if preferred:
        return preferred["category_id"], preferred["category_name"], preferred["aspects"]

    return None, None, aspects


def normalize_measurements(sku, attrs, specs, description, aspects, title_context=""):
    attrs = dict(attrs or {})
    specs = dict(specs or {})
    aspects = dict(aspects or {})
    description = description or ""

    for key, value in (MANUAL_MEASUREMENT_OVERRIDES.get(sku) or {}).items():
        if value not in (None, ""):
            attrs[key] = str(value)
    for key, value in infer_known_layout_measurements(sku, title_context).items():
        if value not in (None, ""):
            attrs[key] = str(value)

    dajian = fetch_dajian_measurements(sku)
    desc_dims = extract_product_dimensions_from_text(description)
    desc_weight = extract_product_weight_from_text(description)

    attr_seed_map = {
        "Assembled Length (in.)": desc_dims.get("length") or dajian.get("assembledLength"),
        "Assembled Width (in.)": desc_dims.get("width") or dajian.get("assembledWidth"),
        "Assembled Height (in.)": desc_dims.get("height") or dajian.get("assembledHeight"),
        "Product Weight (lbs.)": desc_weight or dajian.get("productWeight"),
    }
    for key, value in attr_seed_map.items():
        if value not in (None, "") and not attrs.get(key):
            attrs[key] = str(value)

    package_seed_map = {
        "Package Length (in.)": dajian.get("length"),
        "Package Width (in.)": dajian.get("width"),
        "Package Height (in.)": dajian.get("height"),
        "Package Weight (lbs.)": dajian.get("packageWeight"),
    }
    for key, value in package_seed_map.items():
        if value not in (None, "") and not specs.get(key):
            specs[key] = str(value)

    all_dimensions = extract_all_dimensions(attrs)
    normalized_sources = {
        "Item Length": all_dimensions.get("length"),
        "Item Width": all_dimensions.get("width"),
        "Item Height": all_dimensions.get("height"),
        "Item Weight": all_dimensions.get("weight"),
    }

    extracted = {}
    for aspect_key, raw in normalized_sources.items():
        if raw in (None, ""):
            continue
        value = str(raw).strip()
        if not value:
            continue
        unit = "lbs" if aspect_key == "Item Weight" else "in"
        normalized = value if any(tok in value.lower() for tok in ("in", "lb", "kg", "oz")) else f"{value} {unit}"
        aspects[aspect_key] = [normalized]
        extracted[aspect_key] = normalized

    description_lower = (description or "").lower()
    if "not specified" in description_lower or "not available" in description_lower:
        for aspect_key in ("Item Length", "Item Width", "Item Height", "Item Weight"):
            if aspect_key not in extracted and aspects.get(aspect_key):
                aspects.pop(aspect_key, None)

    for aspect_key in ("Item Length", "Item Width", "Item Height"):
        if aspect_key in extracted:
            continue
        value = first_aspect_text(aspects, aspect_key)
        if value and measurement_issue(value, max_value=500) in {"placeholder", "non-positive", "implausible"}:
            aspects.pop(aspect_key, None)

    if "Item Weight" not in extracted:
        value = first_aspect_text(aspects, "Item Weight")
        if value and measurement_issue(value, max_value=2000) in {"placeholder", "non-positive", "implausible"}:
            aspects.pop("Item Weight", None)

    dims_values = [
        normalized_sources.get("Item Length"),
        normalized_sources.get("Item Width"),
        normalized_sources.get("Item Height"),
    ]
    weight_value = normalized_sources.get("Item Weight")

    if description:
        try:
            description = replace_description_measurements(
                description,
                length=float(dims_values[0]) if dims_values[0] else None,
                width=float(dims_values[1]) if dims_values[1] else None,
                height=float(dims_values[2]) if dims_values[2] else None,
                weight=float(weight_value) if weight_value else None,
            )
        except Exception:
            pass

    return attrs, specs, aspects, description, extracted


def apply_category_defaults(category_id, aspects):
    config = EbayPublisher.CATEGORY_REQUIRED_ASPECTS.get(str(category_id), {})
    defaults = config.get("defaults", {})
    required = config.get("required", [])
    for key in required:
        if key in {"Item Length", "Item Width", "Item Height", "Item Weight"}:
            continue
        if aspects.get(key):
            continue
        default = defaults.get(key)
        if key == "Upholstery Fabric":
            supported_upholstery = first_aspect_value(aspects, "Upholstery Material")
            if supported_upholstery and supported_upholstery.lower() not in {
                "does not apply", "not applicable", "n/a", "unknown"
            }:
                default = supported_upholstery
        if default is not None:
            aspects[key] = ensure_list_value(default)
    return aspects


def first_aspect_value(aspects, key):
    values = ensure_list_value((aspects or {}).get(key))
    return values[0] if values else ""


def set_aspect_value(aspects, key, value):
    if value is None:
        return
    current = first_aspect_value(aspects, key)
    if current == value:
        return
    aspects[key] = [value]


def normalize_semantic_aspects(title, category_id, aspects):
    aspects = dict(aspects or {})
    title_lower = (title or "").lower()

    chair_only_title = "chair" in title_lower and not any(
        token in title_lower for token in ("sofa", "couch", "loveseat", "sectional", "futon", "recliner")
    )

    if any(token in title_lower for token in ("rocking chair", "rocker")):
        set_aspect_value(aspects, "Type", "Rocking Chair")
    elif any(token in title_lower for token in ("egg chair", "hanging egg chair", "hanging swing chair", "swing chair")):
        set_aspect_value(aspects, "Type", "Hanging Chair")
        if str(category_id) == "79682" and not first_aspect_value(aspects, "Room"):
            set_aspect_value(aspects, "Room", "Outdoor")

    if chair_only_title and any(
        token in title_lower for token in ("compressible", "round chair", "cushioned backrest")
    ):
        current_type = first_aspect_value(aspects, "Type").lower()
        if current_type in {"office chair", "task chair", ""}:
            set_aspect_value(aspects, "Type", "Accent Chair")

    return aspects


def normalize_description_semantics(title, description):
    if not description:
        return description

    title_lower = (title or "").lower()
    chair_only_title = "chair" in title_lower and not any(
        token in title_lower for token in ("sofa", "couch", "loveseat", "sectional", "futon", "recliner")
    )
    if not chair_only_title:
        return description

    normalized = description
    normalized = re.sub(r"\bcompressed sofa\b", "compressed chair", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bthis sofa\b", "this chair", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bthe sofa\b", "the chair", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bsofa combines\b", "chair combines", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bcouch\b", "chair", normalized, flags=re.IGNORECASE)
    return normalized


def append_dimension_image_note(sku, description, title="", attrs=None):
    note = build_dimension_image_note(sku, title, attrs or {})
    if not note or not description:
        return description
    if note in description:
        return description
    html_note = f'<p><strong>Dimension Note:</strong> {note}</p>'
    if "</body>" in description.lower():
        return re.sub(r"</body>", html_note + "</body>", description, count=1, flags=re.IGNORECASE)
    return description + html_note


def audit_and_fix_ready_drafts(
    sku_filter: str | None = None,
    *,
    include_error: bool = False,
    mark_fixed_ready: bool = False,
):
    conn = sqlite3.connect(DB_PATH, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 120000")
    matcher = create_category_matcher("PRODUCTION")
    statuses = list(READY_LIKE_STATUSES)
    if include_error:
        statuses.append("ERROR")
    status_placeholders = ",".join("?" for _ in statuses)

    if sku_filter:
        rows = conn.execute(
            "SELECT id, sku, title, description, attributes, specs, optimization, logs, images, videos, url, status "
            f"FROM collected_products WHERE sku = ? AND status IN ({status_placeholders})",
            (sku_filter, *statuses),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, sku, title, description, attributes, specs, optimization, logs, images, videos, url, status "
            f"FROM collected_products WHERE status IN ({status_placeholders}) ORDER BY updated_at DESC",
            statuses,
        ).fetchall()

    report = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_ready_like": len(rows),
        "statuses": statuses,
        "changed": [],
        "unresolved": [],
        "marked_ready": [],
        "qc_results": [],
    }

    for row in rows:
        sku = row["sku"]
        attrs = parse_json_object(row["attributes"])
        specs = parse_json_object(row["specs"])
        opt = parse_json_object(row["optimization"])
        logs = parse_json_list(row["logs"])
        original_attrs = dict(attrs)
        original_specs = dict(specs)
        original_opt = json.loads(json.dumps(opt, ensure_ascii=False)) if opt else {}
        original_aspects = dict(opt.get("aspects") or {})
        original_images = parse_json_list(row["images"])
        original_videos = parse_json_list(row["videos"])
        source_title = normalize_known_title(sku, (row["title"] or "").strip())
        copy_repair_notes = apply_targeted_copy_repairs(sku, opt)
        seed_aspects = dict(opt.get("aspects") or {})
        draft_title = normalize_known_title(sku, (opt.get("title") or row["title"] or "").strip())
        if draft_title:
            opt["title"] = draft_title
        draft_desc = (opt.get("description") or row["description"] or "").strip()

        normalized_images = normalize_image_list(original_images, max_images=24)
        if len(normalized_images) < 2:
            supplier_images = fetch_dajian_images(sku)
            if len(supplier_images) > len(normalized_images):
                normalized_images = supplier_images
        attrs, specs, aspects, fixed_desc, extracted = normalize_measurements(
            sku,
            attrs,
            specs,
            draft_desc,
            seed_aspects,
            title_context=draft_title or source_title,
        )

        category_id, category_name, completed_aspects = choose_ready_category(
            matcher,
            source_title,
            draft_title,
            fixed_desc or row["description"] or "",
            aspects,
            original_opt.get("categoryId"),
            original_opt.get("categoryName"),
        )
        if completed_aspects:
            aspects = completed_aspects
        if category_id:
            opt["categoryId"] = str(category_id)
        if category_name:
            opt["categoryName"] = category_name

        aspects = normalize_semantic_aspects(draft_title or row["title"], opt.get("categoryId"), aspects)
        aspects = apply_category_defaults(opt.get("categoryId"), aspects)
        aspects = sanitize_placeholder_aspects(
            aspects,
            title=draft_title or row["title"] or "",
            category_id=opt.get("categoryId") or "",
        )
        fixed_desc = normalize_description_semantics(draft_title or row["title"], fixed_desc)
        fixed_desc = append_dimension_image_note(sku, fixed_desc, draft_title or row["title"], attrs)
        opt["aspects"] = aspects
        if fixed_desc:
            opt["description"] = fixed_desc
        opt = normalize_generated_listing(
            opt,
            source_title=source_title,
            source_description=row["description"] or "",
            attributes=attrs,
            specs=specs,
            images=normalized_images,
            videos=original_videos,
            category_matcher=matcher,
        )
        aspects = opt.get("aspects") or {}
        fixed_desc = opt.get("description") or fixed_desc

        changes = []
        measurement_changes = {
            key: value for key, value in extracted.items()
            if original_aspects.get(key, [None])[0] != value
        }
        if measurement_changes:
            changes.append({"measurements": measurement_changes})

        if str(original_opt.get("categoryId") or "") != str(opt.get("categoryId") or ""):
            changes.append({
                "category": {
                    "from": {
                        "id": original_opt.get("categoryId"),
                        "name": original_opt.get("categoryName"),
                    },
                    "to": {
                        "id": opt.get("categoryId"),
                        "name": opt.get("categoryName"),
                    },
                }
            })
        elif not original_opt.get("categoryName") and opt.get("categoryName"):
            changes.append({"category_name": opt.get("categoryName")})

        if (original_opt.get("description") or "") != (opt.get("description") or ""):
            changes.append({"description_measurements": "normalized"})
        if copy_repair_notes:
            changes.append({"copy_repairs": copy_repair_notes})

        aspect_repairs = {}
        for key in ("Type", "Room"):
            old_val = first_aspect_value(original_aspects, key)
            new_val = first_aspect_value(aspects, key)
            if old_val != new_val and new_val:
                aspect_repairs[key] = {"from": old_val, "to": new_val}
        if aspect_repairs:
            changes.append({"aspects": aspect_repairs})

        if normalized_images != original_images:
            changes.append({
                "images": {
                    "from": len(original_images),
                    "to": len(normalized_images),
                }
            })

        missing_measurements = [
            key for key in REQUIRED_MEASUREMENT_ASPECT_KEYS
            if not aspects.get(key)
        ]
        unresolved = {}
        if missing_measurements:
            unresolved["missing_measurements"] = missing_measurements
        if len(normalized_images) <= 1:
            unresolved["image_count"] = len(normalized_images)
        qc_result = run_listing_qc(
            sku=sku,
            candidate=opt,
            source_title=source_title,
            source_description=row["description"] or "",
            source_attributes=attrs,
            source_specs=specs,
            images=normalized_images,
            videos=original_videos,
            category_matcher=matcher,
            fact_sheet_conn=conn,
        )
        quality_blockers = list(qc_result["blockers"])
        report["qc_results"].append(qc_result)

        if quality_blockers:
            unresolved["quality_gate"] = quality_blockers
        if unresolved:
            unresolved["sku"] = sku
            unresolved["status"] = row["status"]
            unresolved["title"] = draft_title or row["title"]
            unresolved["categoryId"] = opt.get("categoryId")
            unresolved["categoryName"] = opt.get("categoryName")
            unresolved["url"] = row["url"]
            report["unresolved"].append(unresolved)

        new_status = row["status"]
        if not unresolved and row["status"] == "ERROR" and mark_fixed_ready:
            new_status = "READY"
            logs.append(f"ERROR draft repaired and marked READY at {datetime.now(timezone.utc).isoformat()}")
            changes.append({"status": {"from": row["status"], "to": new_status}})
            report["marked_ready"].append(sku)

        if (
            changes
            or attrs != original_attrs
            or specs != original_specs
            or opt != original_opt
        ):
            logs.append(f"READY audit fix at {datetime.now(timezone.utc).isoformat()}")
            conn.execute(
                "UPDATE collected_products SET optimization=?, attributes=?, specs=?, images=?, logs=?, status=?, updated_at=? WHERE id=?",
                (
                    json.dumps(opt, ensure_ascii=False),
                    json.dumps(attrs, ensure_ascii=False),
                    json.dumps(specs, ensure_ascii=False),
                    json.dumps(normalized_images, ensure_ascii=False),
                    json.dumps(logs, ensure_ascii=False),
                    new_status,
                    datetime.now(timezone.utc).isoformat(),
                    row["id"],
                ),
            )
            report["changed"].append({
                "sku": sku,
                "changes": changes,
            })

    conn.commit()
    conn.close()

    LOG_DIR.mkdir(exist_ok=True)
    out_path = LOG_DIR / f"ready_draft_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(out_path),
                "changed": len(report["changed"]),
                "unresolved": len(report["unresolved"]),
                "marked_ready": len(report["marked_ready"]),
            },
            ensure_ascii=False,
        )
    )
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Audit & fix READY drafts before publish")
    parser.add_argument("--sku", help="Audit a single SKU only", default=None)
    parser.add_argument("--include-error", action="store_true", help="Also audit ERROR drafts")
    parser.add_argument("--mark-fixed-ready", action="store_true", help="Mark repaired ERROR drafts as READY")
    args = parser.parse_args()
    audit_and_fix_ready_drafts(
        sku_filter=args.sku,
        include_error=args.include_error,
        mark_fixed_ready=args.mark_fixed_ready,
    )
