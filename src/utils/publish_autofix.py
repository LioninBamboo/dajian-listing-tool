import re
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional


MULTI_VALUE_ASPECTS = {
    'Features',
    'Special Feature',
    'California Prop 65 Warning',
    'Room',
    'Department',
    'Care Instructions',
    'Additional Parts Required',
    'Compatible Brand',
    'Suitable For',
}

INVALID_CATEGORY_ERROR_MARKERS = (
    'invalid category',
    'invalid category id',
    'not a leaf category',
    '"errorid":25005',
    '"errorid":20400',
)

EBAY_MAX_ASPECT_VALUE_LEN = 65
PLACEHOLDER_ASPECT_VALUES = {
    'see description',
    'refer to description',
    'refer to product images',
    'refer to photos',
    'not specified',
    'not applicable',
    'n/a',
    'na',
    'unknown',
}

LogFn = Callable[[str], None]


def _emit(log: LogFn | None, message: str) -> None:
    if log is not None:
        log(message)


def normalize_publish_error_message(error_msg: str) -> str:
    return (error_msg or '').replace('\xa0', ' ')


def first_single_aspect_value(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ''
    return re.split(r'\s*[,;/|]\s*', text)[0].strip()


def sanitize_single_value_aspects(
    aspects: MutableMapping[str, Any],
    *,
    log: LogFn | None = None,
    multi_value_aspects: Iterable[str] = MULTI_VALUE_ASPECTS,
) -> MutableMapping[str, Any]:
    multi_value_keys = set(multi_value_aspects)

    for aspect_name, values in list(aspects.items()):
        if aspect_name in multi_value_keys:
            continue

        if isinstance(values, list):
            normalized_values = [str(item).strip() for item in values if str(item).strip()]
            if not normalized_values:
                continue
            fixed = first_single_aspect_value(normalized_values[0])
            if len(normalized_values) > 1 or fixed != normalized_values[0]:
                _emit(log, f'[SANITIZE] {aspect_name}: {values} -> [{fixed}]')
            aspects[aspect_name] = [fixed]
            continue

        fixed = first_single_aspect_value(values)
        if fixed:
            _emit(log, f'[SANITIZE] {aspect_name}: {values} -> [{fixed}]')
            aspects[aspect_name] = [fixed]

    return aspects


def is_placeholder_aspect_value(value: Any) -> bool:
    text = str(value or '').strip()
    if not text:
        return False
    normalized = re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip().lower()
    return (
        normalized in PLACEHOLDER_ASPECT_VALUES
        or normalized.startswith('not specified')
        or 'see description' in normalized
        or normalized.startswith('refer to ')
    )


def _clean_aspect_values(values: Any) -> list[str]:
    raw_values = values if isinstance(values, list) else [values]
    return [str(item).strip() for item in raw_values if str(item).strip()]


def infer_placeholder_replacement(
    aspect_name: str,
    *,
    title: str = '',
    category_id: str = '',
    aspects: Mapping[str, Any] | None = None,
) -> Optional[str]:
    title_lower = (title or '').lower()
    aspect_lower = (aspect_name or '').lower()
    aspects = aspects or {}

    if aspect_lower == 'set includes':
        if 'chair' in title_lower and any(token in title_lower for token in ('dining', 'table set', 'set of')):
            return 'Dining Table & Chairs'
        if 'stool' in title_lower and any(token in title_lower for token in ('dining', 'table set', 'set of')):
            return 'Dining Table & Stools'
        if any(token in title_lower for token in ('coffee table', 'side table', 'end table', 'console table', 'bistro table', 'patio table', 'dining table')):
            return 'Table'
        if str(category_id or '') in {'38204', '112590'} and 'table' in title_lower:
            return 'Table'
        if any(token in title_lower for token in ('sectional', 'sofa', 'couch', 'loveseat')):
            return 'Sofa'
        if 'ottoman' in title_lower:
            return 'Ottoman'
        if 'bed' in title_lower:
            return 'Bed Frame'
        if 'ceiling fan' in title_lower:
            return 'Ceiling Fan'

    if aspect_lower in {'weight', 'product weight'}:
        item_weight = _clean_aspect_values(aspects.get('Item Weight'))
        if item_weight and not is_placeholder_aspect_value(item_weight[0]):
            return item_weight[0]

    return None


def sanitize_placeholder_aspects(
    aspects: MutableMapping[str, Any],
    *,
    title: str = '',
    category_id: str = '',
    log: LogFn | None = None,
) -> MutableMapping[str, Any]:
    """Remove or replace generic placeholder item specifics before publish."""
    for aspect_name, values in list(aspects.items()):
        normalized_values = _clean_aspect_values(values)
        if not normalized_values:
            aspects.pop(aspect_name, None)
            continue

        kept_values = [value for value in normalized_values if not is_placeholder_aspect_value(value)]
        if kept_values:
            if kept_values != normalized_values:
                _emit(log, f'[SANITIZE] Removed placeholder values from {aspect_name}: {values} -> {kept_values}')
            aspects[aspect_name] = kept_values
            continue

        replacement = infer_placeholder_replacement(
            aspect_name,
            title=title,
            category_id=category_id,
            aspects=aspects,
        )
        if replacement and not is_placeholder_aspect_value(replacement):
            aspects[aspect_name] = [replacement]
            _emit(log, f'[SANITIZE] {aspect_name}: placeholder {values} -> [{replacement}]')
            continue

        aspects.pop(aspect_name, None)
        _emit(log, f'[SANITIZE] Removed placeholder aspect {aspect_name}: {values}')

    return aspects


def is_invalid_category_error(error_msg: str) -> bool:
    normalized = normalize_publish_error_message(error_msg).lower()
    return any(marker in normalized for marker in INVALID_CATEGORY_ERROR_MARKERS)


def try_fix_publish_error(
    error_msg: str,
    aspects: MutableMapping[str, Any],
    category_id: str,
    category_required_aspects: Mapping[str, Mapping[str, Any]],
    measurement_aspect_keys: Iterable[str],
    *,
    log: LogFn | None = None,
) -> bool:
    normalized_error = normalize_publish_error_message(error_msg)
    measurement_keys = {str(key).strip() for key in measurement_aspect_keys if str(key).strip()}

    match = re.search(r'The item specific ([\w\s/]+?) is missing', normalized_error)
    if match:
        missing = match.group(1).strip()
        _emit(log, f'[AUTO-FIX] Missing: {missing}')

        if missing in measurement_keys:
            _emit(log, f'[AUTO-FIX] Refusing placeholder fallback for measurement aspect {missing}')
            return False

        defaults = category_required_aspects.get(category_id, {}).get('defaults', {})
        if missing in defaults:
            value = defaults[missing]
            aspects[missing] = [value] if isinstance(value, str) else value
            _emit(log, f'[AUTO-FIX] {missing} = {aspects[missing]}')
            return True

        _emit(log, f'[AUTO-FIX] No safe default for missing aspect {missing}; refusing generic placeholder')
        return False

    match = re.search(r'([\w\s/]+?) should contain only one value', normalized_error)
    if match:
        field = match.group(1).strip()
        if field in aspects:
            current = aspects[field]
            if isinstance(current, list):
                if not current:
                    return False
                first = first_single_aspect_value(current[0])
            else:
                first = first_single_aspect_value(current)

            if first:
                _emit(log, f"[AUTO-FIX] {field}: forcing single value '{first}'")
                aspects[field] = [first]
                return True

    match = re.search(r"([\w\s/]+?)'s value of \"(.+?)\" is too long", normalized_error)
    if not match:
        match = re.search(r"([\w\s/]+?)'s value of \"(.+?)\" has too many characters", normalized_error)
    if match:
        field = match.group(1).strip()
        long_value = match.group(2).strip()
        _emit(log, f'[AUTO-FIX] {field}: value too long ({len(long_value)} chars)')

        if field in aspects:
            current = aspects[field]
            values = current if isinstance(current, list) else [current]
            for index, value in enumerate(values):
                if len(str(value)) > EBAY_MAX_ASPECT_VALUE_LEN:
                    numbers = re.findall(r'[\d,.]+\s*(?:lbs?|kg|oz|in|cm|mm|ft)?', str(value))
                    if numbers and any(keyword in field.lower() for keyword in ('capacity', 'weight', 'load')):
                        truncated = numbers[0].strip()
                        if not any(char.isalpha() for char in truncated):
                            truncated += ' lbs'
                    else:
                        truncated = str(value)[:EBAY_MAX_ASPECT_VALUE_LEN]
                    _emit(log, f"[AUTO-FIX] {field}: '{str(value)[:30]}...' -> '{truncated}'")
                    values[index] = truncated
            aspects[field] = values
            return True

    return False


# ---------------------------------------------------------------------------
# Pre-flight aspect helpers (eBay API limits)
# ---------------------------------------------------------------------------

SINGLE_VALUE_ASPECTS: frozenset[str] = frozenset({
    "Room", "Style", "Color", "Brand", "Type", "Material",
    "Department", "Finish", "Shape", "Pattern", "Country/Region of Manufacture",
    "Theme", "Manufacturer Warranty", "Upholstery Material", "Upholstery Fabric",
    "Warranty", "Mounting", "Indoor/Outdoor", "Assembly Required",
    "Item Width", "Item Height", "Item Length", "Item Depth", "Item Weight",
    "Seating Capacity", "Number of Drawers", "Number of Shelves",
    "Number of Items in Set", "Number of Pieces", "Compatible Mattress Size",
    "Load Capacity", "Maximum Weight Capacity", "Frame Material",
    "Tabletop Material", "Top Material",
    "Back Style", "Arm Style", "Leg Style", "Seat Height", "Seat Width", "Seat Depth",
    "Best for", "Resistance Type", "Sport/Activity",
})

EBAY_MAX_ASPECTS: int = 45

_PRIORITY_ASPECT_KEYS: frozenset[str] = frozenset({
    "Brand", "Type", "Material", "Color", "Style", "Room",
    "Item Width", "Item Height", "Item Length", "Item Weight",
    "Compatible Mattress Size", "Upholstery Fabric", "Upholstery Material",
    "Features", "Assembly Required", "MPN", "Country/Region of Manufacture",
    "Number of Items in Set", "Set Includes", "Number of Pieces",
    "Seating Capacity", "Shape", "Finish", "Department", "Pattern",
    "Frame Material", "Firmness", "Size", "Resistance Type",
})


def truncate_aspect_values(
    aspects: MutableMapping[str, Any],
    *,
    log: LogFn | None = None,
) -> None:
    """Truncate each aspect value in-place to EBAY_MAX_ASPECT_VALUE_LEN (65 chars).

    For capacity/weight fields, attempts to extract the leading numeric portion
    instead of hard-cutting mid-string.
    """
    for k, vals in aspects.items():
        if not isinstance(vals, list):
            continue
        for idx, val in enumerate(vals):
            if not isinstance(val, str) or len(val) <= EBAY_MAX_ASPECT_VALUE_LEN:
                continue
            nums = re.findall(r'[\d,.]+\s*(?:lbs?|kg|oz|in|cm|mm|ft)?', val)
            if nums and k.lower() in ('weight capacity', 'load capacity', 'maximum weight capacity'):
                truncated = nums[0].strip()
                if not any(c.isalpha() for c in truncated):
                    truncated += ' lbs'
                _emit(log, f'[FIX] {k}: extracted {truncated!r} from long value ({len(val)} chars)')
            else:
                truncated = val[:EBAY_MAX_ASPECT_VALUE_LEN]
                _emit(log, f'[FIX] {k}: truncated from {len(val)} to {EBAY_MAX_ASPECT_VALUE_LEN} chars')
            vals[idx] = truncated


def trim_aspects_to_limit(
    aspects: MutableMapping[str, Any],
    required_aspect_names: Iterable[str] = (),
    *,
    max_aspects: int = EBAY_MAX_ASPECTS,
    log: LogFn | None = None,
) -> None:
    """Drop excess aspects beyond *max_aspects* in-place, keeping priority and required ones."""
    if len(aspects) <= max_aspects:
        return
    original_len = len(aspects)
    priority = set(_PRIORITY_ASPECT_KEYS) | {str(n).strip() for n in required_aspect_names if str(n).strip()}
    kept_priority = {k: v for k, v in aspects.items() if k in priority}
    kept_extra = {k: v for k, v in aspects.items() if k not in priority}
    remaining_slots = max_aspects - len(kept_priority)
    trimmed_extra = dict(list(kept_extra.items())[:max(0, remaining_slots)])
    aspects.clear()
    aspects.update({**kept_priority, **trimmed_extra})
    _emit(log, f'[FIX] Trimmed aspects from {original_len} to {len(aspects)} (max {max_aspects})')


def prepare_ebay_aspects(
    aspects: MutableMapping[str, Any],
    required_aspect_names: Iterable[str] = (),
    *,
    log: LogFn | None = None,
) -> None:
    """Final eBay API pre-flight: truncate long values then trim excess aspects.

    Calls truncate_aspect_values then trim_aspects_to_limit in-place.
    """
    truncate_aspect_values(aspects, log=log)
    trim_aspects_to_limit(aspects, required_aspect_names, log=log)
