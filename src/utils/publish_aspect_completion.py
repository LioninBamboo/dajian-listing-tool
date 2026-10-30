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
_TABLE_SET_DOMAIN_RE = re.compile(
    r"\b(?:table|dining|patio|bistro)\b|\bpub set\b|\bconversation set\b",
    re.IGNORECASE,
)
_SET_CONTEXT_RE = re.compile(
    r"\b(?:dining set|table set|set for|set of)\b|"
    r"\b\d{1,3}\s*[- ]?\s*pieces?\b|"
    r"\bpieces?\b|"
    r"table and chair|table\s*&\s*chair",
    re.IGNORECASE,
)
# "in" is inches only when dimension-like, not the preposition in "set of 4 in grey".
_IN_AS_INCHES = (
    r"in\."
    r"|in(?=\s*[x×])"
    r"|in(?=\s*\d)"
    r"|in(?=\s*$)"
    r"|in(?=\s*[,;:)\]}/])"
    r"|in(?=\s+(?:wide|long|tall|high|deep|dia(?:meter)?|width|height|length|thick(?:ness)?)\b)"
)
# Units/size words that must not be read as a seat or piece count.
_DIMENSION_AFTER_NUMBER = (
    rf"inch(?:es)?\b|{_IN_AS_INCHES}|\"|''|ft\b|feet\b|foot\b|cm\b|mm\b|"
    r"lbs?\b|pounds?\b|kg\b|oz\b|seat\s+height\b|tiers?\b|drawers?\b"
)
_REAL_TABLE_RE = re.compile(
    r"\btable\b|\b(?:dining|bistro|pub|patio)\s+(?:table|set)\b|桌",
    re.IGNORECASE,
)
NOT_DIMENSION_AFTER = rf"(?!\s*[-–—]?\s*(?:{_DIMENSION_AFTER_NUMBER}))"
_NOT_DIMENSION_AFTER = NOT_DIMENSION_AFTER
_DIMENSION_AFTER_NUMBER_RE = re.compile(
    rf"\s*[-–—]?\s*(?:{_DIMENSION_AFTER_NUMBER})",
    re.IGNORECASE,
)
# 0-3 adjective gap only after "with". Bare "N chairs" stays tight; one
# non-unit adjective is allowed only for dining/upholstered chairs.
_SEATING_COUNT_PATTERNS = (
    rf"\bwith\s+(\d{{1,3}}){_NOT_DIMENSION_AFTER}\s+(?:[a-z]+\s+){{0,3}}?(?:chairs?|stools?)\b",
    rf"\b(\d{{1,3}}){_NOT_DIMENSION_AFTER}\s+(?:barrel\s+)?chairs?\b",
    rf"\b(\d{{1,3}}){_NOT_DIMENSION_AFTER}\s+stools?\b",
    rf"\b(\d{{1,3}}){_NOT_DIMENSION_AFTER}\s+(?:dining|upholstered)\s+chairs?\b",
)
_SEATING_PLUS_ONE_PATTERNS = _SEATING_COUNT_PATTERNS + (
    rf"\bseats?\s+(\d{{1,3}}){_NOT_DIMENSION_AFTER}\b",
)
_PLAUSIBLE_SEAT_MIN = 1
_PLAUSIBLE_SEAT_MAX = 12
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
    "microsuede": "Microfiber",
    "chenille": "Chenille",
    # Source-grounded free-text values. Do NOT map plush/foam → Microfiber:
    # inventing Microfiber fails FactSheet semantic_material CRITICAL when the
    # supplier only lists foam / plush fabric / spring.
    "plush fabric": "Plush Fabric",
    "plush": "Plush Fabric",
}


def has_table_set_domain(title: str) -> bool:
    return bool(_TABLE_SET_DOMAIN_RE.search(title or ""))


def has_real_table(text: str) -> bool:
    """True only for a table / dining|bistro|pub|patio table|set / 桌."""
    return bool(_REAL_TABLE_RE.search(text or ""))


def number_is_dimension_bound(text: str, match: re.Match[str], group: int = 1) -> bool:
    """True when the captured number is a size/model, not a count."""
    if match.group(group) is None:
        return False
    start = match.start(group)
    if start > 0:
        prev = text[start - 1]
        if prev in "/\\" or (prev.isascii() and prev.isalnum()):
            return True
    return bool(_DIMENSION_AFTER_NUMBER_RE.match(text[match.end(group):]))


def plausible_seat_count(value: int) -> bool:
    return _PLAUSIBLE_SEAT_MIN <= value <= _PLAUSIBLE_SEAT_MAX


def _plausible_seat_count(value: int) -> bool:
    return plausible_seat_count(value)


def seating_item_counts(text: str) -> set[int]:
    """N from 'N chairs/stools' phrases. Does not add the table."""
    lowered = (text or "").lower()
    counts: set[int] = set()
    for seating_pattern in _SEATING_COUNT_PATTERNS:
        for match in re.finditer(seating_pattern, lowered):
            if number_is_dimension_bound(lowered, match):
                continue
            try:
                value = int(match.group(1))
            except ValueError:
                continue
            if _plausible_seat_count(value):
                counts.add(value)
    return counts


def seating_plus_table_item_count(text: str) -> Optional[str]:
    """Table/dining + N chairs/stools => N+1, same seating patterns as dining sets."""
    lowered = (text or "").lower()
    for seating_pattern in _SEATING_PLUS_ONE_PATTERNS:
        match = re.search(seating_pattern, lowered)
        if not match or number_is_dimension_bound(lowered, match):
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            return None
        if not _plausible_seat_count(value):
            continue
        return str(value + 1)
    return None


def infer_number_of_items_in_set(title: str, *, category_id: str = "") -> Optional[str]:
    text = (title or "").lower()

    for pattern in (
        rf"\b(\d+){_NOT_DIMENSION_AFTER}\s*-\s*piece\b",
        rf"\b(\d+){_NOT_DIMENSION_AFTER}\s+piece\b",
        rf"\b(\d+){_NOT_DIMENSION_AFTER}\s*pc\b",
        rf"\bset\s+of\s+(\d+){_NOT_DIMENSION_AFTER}\b",
    ):
        match = re.search(pattern, text)
        if match and not number_is_dimension_bound(text, match):
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
    return seating_plus_table_item_count(title)


def _emit(log: Optional[Callable[[str], None]], message: str) -> None:
    if callable(log):
        log(message)


def infer_set_includes(title: str) -> Optional[str]:
    title_lower = (title or "").lower()
    if any(keyword in title_lower for keyword in TABLE_TENNIS_KEYWORDS):
        return "Table"
    set_context = bool(_SET_CONTEXT_RE.search(title_lower))
    has_chairs = bool(re.search(r"\bchairs?\b", title_lower))
    has_stools = bool(re.search(r"\bstools?\b", title_lower))
    table_and_chairs = (
        "table and chair" in title_lower or "table & chair" in title_lower
    )
    if has_chairs and set_context and has_table_set_domain(title_lower):
        if table_and_chairs or (has_real_table(title_lower) and seating_item_counts(title_lower)):
            return "Dining Table & Chairs" if "dining" in title_lower else "Table & Chairs"
        return "Chairs"
    if has_stools and set_context and has_table_set_domain(title_lower):
        if has_real_table(title_lower) and (
            seating_item_counts(title_lower) or table_and_chairs
        ):
            return "Dining Table & Stools" if "dining" in title_lower else "Table & Stools"
        if has_real_table(title_lower):
            return "Dining Table & Stools" if "dining" in title_lower else "Table & Stools"
    if "sofa" in title_lower or "couch" in title_lower or "sectional" in title_lower:
        return "Sofa Set"
    if set_context:
        if not has_table_set_domain(title_lower):
            return None
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
    # Longest / most specific keywords first (map order is insertion order).
    for keyword, value in FABRIC_KEYWORDS.items():
        if keyword in check_text:
            return value
    # Bare "fabric" / foam fill is not evidence of a specific fiber. Prefer a
    # generic Fabric claim over inventing Polyester/Microfiber (both trip
    # FactSheet CRITICAL when the source only lists foam/plush fabric/spring).
    if "fabric" in check_text:
        return "Fabric"
    return "Fabric"


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
