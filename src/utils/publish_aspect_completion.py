import re
from typing import Any, Callable, Dict, Optional

from src.utils.dimension_helpers import (
    extract_product_weight_from_text,
    find_weight,
    populate_dimension_aspects,
)
from src.utils.publish_validation import (
    MEASUREMENT_ASPECT_KEYS,
    first_aspect_text,
    measurement_issue,
)


BED_CATEGORIES = {"175754", "175758", "175756", "131604"}
SET_INCLUDES_SKIP_CATEGORIES = {"48319"}
MEASUREMENT_DEFAULT_SKIP = set(MEASUREMENT_ASPECT_KEYS) | {"Item Weight"}
SET_INCLUDE_KEYWORDS = ("dining set", "table set", "piece", "set for", "set of")
TABLE_TENNIS_KEYWORDS = ("table tennis", "ping pong")
BED_SIZE_MAP = {
    "twin xl": "Twin XL",
    "california king": "California King",
    "twin": "Twin",
    "full": "Full",
    "queen": "Queen",
    "king": "King",
}
FABRIC_KEYWORDS = {
    "faux leather": "Faux Leather",
    "pu leather": "Faux Leather",
    "velvet": "Velvet",
    "linen": "Linen",
    "corduroy": "Corduroy",
    "leather": "Leather",
    "polyester": "Polyester",
    "cotton": "Cotton",
    "microfiber": "Microfiber",
}


def infer_number_of_items_in_set(title: str, *, category_id: str = "") -> Optional[str]:
    text = (title or "").lower()

    for pattern in (
        r"\b(\d+)\s*-\s*piece\b",
        r"\b(\d+)\s+piece\b",
        r"\b(\d+)\s*pc\b",
        r"\bset\s+of\s+(\d+)\b",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    is_dining_set = (
        not any(keyword in text for keyword in TABLE_TENNIS_KEYWORDS)
        and (
            str(category_id or "") == "107578"
            or "dining set" in text
            or "table set" in text
        )
    )
    if not is_dining_set:
        return None

    for seating_pattern in (
        r"\bwith\s+(\d+)\s+(?:[a-z]+\s+){0,3}?chairs?\b",
        r"\bwith\s+(\d+)\s+(?:[a-z]+\s+){0,3}?stools?\b",
        r"\b(\d+)\s+(?:barrel\s+)?chairs?\b",
        r"\b(\d+)\s+stools?\b",
        r"\bseats?\s+(\d+)\b",
    ):
        match = re.search(seating_pattern, text)
        if not match:
            continue
        try:
            return str(int(match.group(1)) + 1)
        except ValueError:
            return None

    return None


def _emit(log: Optional[Callable[[str], None]], message: str) -> None:
    if callable(log):
        log(message)


def infer_set_includes(title: str) -> Optional[str]:
    title_lower = (title or "").lower()
    if any(keyword in title_lower for keyword in TABLE_TENNIS_KEYWORDS):
        return "Table"
    set_context = (
        any(keyword in title_lower for keyword in SET_INCLUDE_KEYWORDS)
        or "table and chair" in title_lower
        or "table & chair" in title_lower
    )
    if "chair" in title_lower and set_context:
        if "table and chair" not in title_lower and "table & chair" not in title_lower:
            return "Chairs"
        return "Dining Table & Chairs" if "dining" in title_lower else "Table & Chairs"
    if "stool" in title_lower and set_context:
        return "Dining Table & Stools" if "dining" in title_lower else "Table & Stools"
    if "sofa" in title_lower or "couch" in title_lower or "sectional" in title_lower:
        return "Sofa Set"
    if any(keyword in title_lower for keyword in SET_INCLUDE_KEYWORDS):
        return "Table Set"
    if any(keyword in title_lower for keyword in ("coffee table", "side table", "end table", "console table", "bistro table", "patio table", "dining table")):
        return "Table"
    return None


def infer_bed_size(title: str, category_id: str) -> str:
    title_lower = (title or "").lower()
    for keyword, value in BED_SIZE_MAP.items():
        if keyword in title_lower:
            return value
    return "Twin" if str(category_id or "") == "175754" else "Queen"


def infer_upholstery_fabric(title: str, aspects: Dict[str, Any]) -> str:
    material = ""
    existing_material = aspects.get("Material") if isinstance(aspects, dict) else None
    if isinstance(existing_material, list) and existing_material:
        material = str(existing_material[0]).lower()
    elif existing_material:
        material = str(existing_material).lower()

    check_text = f"{material} {(title or '').lower()}"
    for keyword, value in FABRIC_KEYWORDS.items():
        if keyword in check_text:
            return value
    return "Polyester"


def _extract_title_length(title: str) -> Optional[str]:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:inches?|inch|in)\b", (title or "").lower())
    if not match:
        return None
    return match.group(1)


def complete_publish_aspects(
    aspects: Dict[str, Any],
    *,
    title: str,
    category_id: str,
    attrs: Optional[Dict[str, Any]] = None,
    description: str = "",
    category_required_aspects: Optional[Dict[str, Dict[str, Any]]] = None,
    log: Optional[Callable[[str], None]] = None,
    ensure_required_dimensions: bool = True,
    fill_item_weight: bool = True,
) -> Dict[str, Any]:
    completed = dict(aspects or {})
    attrs = attrs if isinstance(attrs, dict) else {}
    title_lower = (title or "").lower()
    cat_id = str(category_id or "")
    category_required_aspects = category_required_aspects or {}

    added_dims = populate_dimension_aspects(completed, attrs)
    if added_dims:
        _emit(log, f"[DIMS] Auto-filled: {added_dims}")

    if cat_id in SET_INCLUDES_SKIP_CATEGORIES:
        completed.pop("Set Includes", None)
    elif not completed.get("Set Includes"):
        set_includes = infer_set_includes(title_lower)
        if set_includes:
            completed["Set Includes"] = [set_includes]
            _emit(log, f"[AUTO] Set Includes = {set_includes}")

    inferred_item_count = infer_number_of_items_in_set(title_lower, category_id=cat_id)
    existing_item_count = first_aspect_text(completed, "Number of Items in Set") or first_aspect_text(completed, "Number of Pieces")
    if inferred_item_count and not existing_item_count:
        completed["Number of Items in Set"] = [inferred_item_count]
        completed["Number of Pieces"] = [inferred_item_count]
        _emit(log, f"[AUTO] Number of Items in Set = {inferred_item_count}")
        _emit(log, f"[AUTO] Number of Pieces = {inferred_item_count}")
    elif cat_id == "107578" and existing_item_count:
        if not completed.get("Number of Items in Set"):
            completed["Number of Items in Set"] = [existing_item_count]
            _emit(log, f"[SYNC] Number of Items in Set = {existing_item_count}")
        if not completed.get("Number of Pieces"):
            completed["Number of Pieces"] = [existing_item_count]
            _emit(log, f"[SYNC] Number of Pieces = {existing_item_count}")

    if cat_id in BED_CATEGORIES and not completed.get("Compatible Mattress Size"):
        detected_size = infer_bed_size(title_lower, cat_id)
        completed["Compatible Mattress Size"] = [detected_size]
        _emit(log, f"[AUTO] Compatible Mattress Size = {detected_size}")

    if cat_id == "38208" and not completed.get("Upholstery Fabric"):
        fabric = infer_upholstery_fabric(title_lower, completed)
        completed["Upholstery Fabric"] = [fabric]
        _emit(log, f"[AUTO] Upholstery Fabric = {fabric}")

    defaults = category_required_aspects.get(cat_id, {}).get("defaults", {})
    for aspect_name, default_value in defaults.items():
        if aspect_name in MEASUREMENT_DEFAULT_SKIP:
            continue
        if completed.get(aspect_name):
            continue
        completed[aspect_name] = [default_value] if isinstance(default_value, str) else default_value
        _emit(log, f"[DEFAULT] {aspect_name} = {completed[aspect_name]}")

    if ensure_required_dimensions:
        title_length = _extract_title_length(title_lower)
        for dimension_key in ("Item Length", "Item Width", "Item Height"):
            if completed.get(dimension_key):
                continue
            if dimension_key == "Item Length" and title_length:
                completed[dimension_key] = [f"{title_length} in"]
                _emit(log, f"[TITLE-DIM] {dimension_key} = {completed[dimension_key]}")
                continue
            completed[dimension_key] = ["See Description"]
            _emit(log, f"[FALLBACK] {dimension_key} = See Description")

    if fill_item_weight:
        existing_weight = first_aspect_text(completed, "Item Weight")
        if existing_weight and measurement_issue(existing_weight) in {"placeholder", "non-positive"}:
            completed.pop("Item Weight", None)
            _emit(log, f"[DROP] Ignoring invalid Item Weight = {existing_weight}")

        if not completed.get("Item Weight"):
            fallback_weight = find_weight(attrs) or extract_product_weight_from_text(description or "")
            if fallback_weight:
                completed["Item Weight"] = [f"{fallback_weight} lbs"]
                _emit(log, f"[WEIGHT] Item Weight = {completed['Item Weight']}")

    return completed
