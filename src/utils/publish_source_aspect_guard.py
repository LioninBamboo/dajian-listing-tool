"""Source-grounded item-specific guard used at publish time.

After template fill + placeholder sanitization, every final aspect except
dimensions/weight, Brand, and MPN is checked against the GIGA source blob
(title, description, attributes, specs). eBay sentinel values and any
aspect the listing quality gate already requires or derives are kept.
Unsourced optional claims (materials, voltage/power, warranty, …) are
stripped; unsourced required aspects are replaced with a source-matching
value when one exists, otherwise they block publish.

Draft precedence applies to Type and free-text descriptive aspects only:
a seedable non-empty draft value wins over quality-gate / source /
template auto-fill. Gate-derived hard aspects (counts, assembly, room,
set includes, indoor/outdoor) never silently keep a disagreeing draft
or silently swap to the gate — they block as
``aspect_draft_conflicts_gate:<Name>``.

Draft values are seeded only when source-supported, or when they exactly
match a SELECTION_ONLY allowed list and the upstream field is empty.
Otherwise the upstream value is kept and the skip is logged.
SELECTION_ONLY validates every list value; any not-allowed value blocks
as ``aspect_value_not_allowed`` (no nearest-guess, no silent truncate).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional

from src.utils.publish_validation import MEASUREMENT_ASPECT_KEYS, first_aspect_text


LogFn = Callable[[str], None]

SKIP_ASPECT_NAMES = {key.lower() for key in MEASUREMENT_ASPECT_KEYS} | {
    "item weight",
    "package weight",
    "package length",
    "package width",
    "package height",
    "brand",
    "mpn",
    "manufacturer part number",
}

EBAY_SENTINEL_VALUES = {
    "does not apply",
    "doesnotapply",
    "unbranded",
    "n/a",
    "n.a",
    "n.a.",
    "na",
    "n a",
    "none",
    "not applicable",
    "not available",
    "unspecified",
    "unknown",
}

POWER_ASPECT_MARKERS = (
    "battery",
    "powered",
    "light-up",
    "light up",
    "lightup",
    "voltage",
    "remote",
)

_STOPWORDS = {
    "a", "an", "and", "for", "in", "of", "or", "the", "to", "with",
    "does", "not", "apply",
}

_SIMPLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "wood": ("wood", "wooden"),
    "wooden": ("wood", "wooden"),
    "metal": ("metal", "metallic"),
    "metallic": ("metal", "metallic"),
    "glass": ("glass", "glassy"),
    "silver": ("silver", "silvery"),
    "gray": ("gray", "grey"),
    "grey": ("gray", "grey"),
    "fabric": ("fabric", "textile"),
    "cabinet": ("cabinet", "cabinets"),
    "vanity": ("vanity", "vanities"),
    "mirror": ("mirror", "mirrors"),
    "table": ("table", "tables"),
    "sofa": ("sofa", "sofas", "couch", "couches"),
    "couch": ("sofa", "sofas", "couch", "couches"),
}

_COMMON_COLORS = (
    "black", "white", "gray", "grey", "brown", "red", "blue", "green",
    "silver", "gold", "beige", "natural", "walnut", "oak", "ivory",
    "navy", "pink", "purple", "orange", "yellow", "chrome", "clear",
)

_COMMON_MATERIALS = (
    "solid wood", "engineered wood", "tempered glass", "stainless steel",
    "faux leather", "wood", "metal", "glass", "steel", "iron", "plastic",
    "fabric", "leather", "mdf", "marble", "ceramic", "rattan", "wicker",
    "bamboo", "acrylic", "resin", "velvet", "linen", "cotton",
)

_COMMON_TYPES = (
    "bathroom vanity", "coffee table", "apothecary cabinet", "display cabinet",
    "medicine cabinet", "side table", "end table", "dining table",
    "wall mirror", "floor mirror", "bathroom mirror", "vanity", "cabinet",
    "mirror", "sofa", "table",
)


def _emit(log: Optional[LogFn], message: str) -> None:
    if callable(log):
        log(message)


def _flatten_mapping(blob: Any) -> list[str]:
    parts: list[str] = []
    if isinstance(blob, Mapping):
        for key, value in blob.items():
            if isinstance(value, Mapping):
                parts.extend(_flatten_mapping(value))
            elif isinstance(value, (list, tuple)):
                parts.append(f"{key} {' '.join(str(item) for item in value)}")
            elif value is not None:
                parts.append(f"{key} {value}")
    elif isinstance(blob, (list, tuple)):
        parts.extend(str(item) for item in blob if item is not None)
    elif blob:
        parts.append(str(blob))
    return parts


def build_giga_source_text(
    title: str = "",
    description: str = "",
    attributes: Any = None,
    specs: Any = None,
) -> str:
    desc = re.sub(r"<[^>]+>", " ", description or "")
    parts = [title or "", desc]
    parts.extend(_flatten_mapping(attributes))
    parts.extend(_flatten_mapping(specs))
    return re.sub(r"\s+", " ", " ".join(str(part) for part in parts if part)).strip()


def _normalize_text(value: str) -> str:
    return re.sub(r"[\s/_-]+", " ", str(value or "").lower()).strip()


def _tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", _normalize_text(value)) if token]


def _plural_variants(token: str) -> set[str]:
    variants = {token}
    if token.endswith("ies") and len(token) > 3:
        variants.add(token[:-3] + "y")
    elif token.endswith("es") and len(token) > 3:
        variants.add(token[:-2])
        variants.add(token[:-1])
    elif token.endswith("s") and len(token) > 2:
        variants.add(token[:-1])
    else:
        variants.add(token + "s")
        if token.endswith("y") and len(token) > 2 and token[-2] not in "aeiou":
            variants.add(token[:-1] + "ies")
        elif token.endswith(("s", "x", "z")) or token.endswith(("ch", "sh")):
            variants.add(token + "es")
    variants.update(_SIMPLE_SYNONYMS.get(token, ()))
    return variants


def _phrase_in_source(phrase: str, source_text: str) -> bool:
    phrase_norm = _normalize_text(phrase)
    source_norm = _normalize_text(source_text)
    if not phrase_norm or not source_norm:
        return False
    if re.search(rf"(?<![a-z0-9]){re.escape(phrase_norm)}(?![a-z0-9])", source_norm):
        return True
    tokens = [token for token in _tokens(phrase_norm) if token not in _STOPWORDS]
    if not tokens:
        return False
    source_tokens = set(_tokens(source_norm))
    source_token_variants = set(source_tokens)
    for source_token in source_tokens:
        source_token_variants.update(_plural_variants(source_token))
    return all(
        any(variant in source_token_variants for variant in _plural_variants(token))
        for token in tokens
    )


def value_supported_by_source(value: str, source_text: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return _phrase_in_source(text, source_text)


def is_skipped_aspect(name: str) -> bool:
    return str(name or "").strip().lower() in SKIP_ASPECT_NAMES


def is_ebay_sentinel_value(value: str) -> bool:
    return _normalize_text(value) in EBAY_SENTINEL_VALUES


def _expected_value_for(
    name: str,
    expected_values_by_name: Optional[Mapping[str, Any]] = None,
) -> str:
    if not expected_values_by_name:
        return ""
    want = str(name or "").strip().lower()
    for key, raw in expected_values_by_name.items():
        if str(key or "").strip().lower() != want:
            continue
        values = _aspect_values(raw)
        if values:
            return values[0]
    return ""


def _values_match_expected(values: Iterable[str], expected: str) -> bool:
    want = _normalize_text(expected)
    return bool(want) and any(_normalize_text(value) == want for value in values)


def is_power_aspect(name: str) -> bool:
    normalized = _normalize_text(name)
    return any(marker in normalized for marker in POWER_ASPECT_MARKERS)


def _aspect_values(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    return [text] if text else []


# Material aspects: exact name "Material", names ending with " Material",
# or this whole-name list. No substring guesses on the aspect name.
_MATERIAL_ASPECT_EXACT_NAMES = frozenset({
    "upholstery fabric",
    "fabric type",
    "filling",
    "filler",
    "wood type",
    "primary material",
    "材质",
    "主材质",
})

# Source token -> eBay allowed values in preference order. Whole-token
# equality after normalization only; never invent a value not in the list.
_MATERIAL_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "mdf": ("MDF/Chipboard",),
    "medium density fiberboard": ("MDF/Chipboard",),
    "medium density fibreboard": ("MDF/Chipboard",),
    "particleboard": ("MDF/Chipboard",),
    "particle board": ("MDF/Chipboard",),
    "chipboard": ("MDF/Chipboard",),
    "rubberwood": ("Rubberwood", "Solid Wood"),
    "rubber wood": ("Rubberwood", "Solid Wood"),
    "eucalyptus": ("Eucalyptus", "Solid Wood"),
    "acacia": ("Acacia", "Solid Wood"),
}

_STRUCTURED_MATERIAL_KEYS = {
    "material": "generic",
    "main material": "generic",
    "primary material": "generic",
    "材质": "generic",
    "主材质": "generic",
    "top material": "countertop",
    "countertop material": "countertop",
    "tabletop material": "countertop",
    "table top material": "countertop",
    "desktop material": "countertop",
    "base material": "base",
    "frame material": "frame",
    "seat material": "seat",
    "back material": "back",
    "leg material": "leg",
    "upholstery material": "upholstery",
    "upholstery fabric": "upholstery",
}

_PART_PREFIX_ALIASES = {
    "countertop": "countertop",
    "top": "countertop",
    "tabletop": "countertop",
    "table top": "countertop",
    "desktop": "countertop",
    "worktop": "countertop",
    "base": "base",
    "bottom": "base",
    "frame": "frame",
    "seat": "seat",
    "back": "back",
    "leg": "leg",
    "legs": "leg",
    "upholstery": "upholstery",
    "fabric": "upholstery",
    "main": "generic",
    "primary": "generic",
}

_COLORISH_MATERIAL_TOKENS = frozenset({
    "bronze",
    "gold",
    "brass",
    "black",
    "silver",
    "white",
    "brown",
    "gray",
    "grey",
    "chrome",
    "copper",
    "nickel",
    "antique bronze",
})

_HARDWARE_FINISH_AFTER = re.compile(
    r"^(?:[\s/-]+)?(?:and\s+)?(?:"
    r"hardware|handles?|pulls?|knobs?|hinges?|"
    r"leg\s+caps?|accents?|trims?|finish(?:es|ed)?|"
    r"colou?rs?|tones?|toned"
    r")\b"
)
_NEGATION_BEFORE = re.compile(
    r"(?:^|[^a-z0-9])(?:no|not|without)\s+$|(?:^|[^a-z0-9])non[\s-]$"
)
_MADE_OF_BEFORE = re.compile(
    r"\b(?:made|constructed|built|crafted)\s+(?:of|from)\s+$"
)
_GENERIC_MATERIAL_LABEL_BEFORE = re.compile(
    r"\b(?:main\s+|primary\s+)?material\s*[:：]?\s+$"
)
_COUNTERTOP_AFTER = re.compile(
    r"^(?:[\s/-]+)?(?:counter\s*tops?|table\s*tops?|desktops?|worktops?)\b"
    r"|^(?:[\s/-]+)?tops?\b(?!\s+(?:quality|rated|grade|tier|of)\b)"
)
_COUNTERTOP_BEFORE = re.compile(
    r"\b(?:counter\s*tops?|table\s*tops?|desktops?|worktops?)"
    r"(?:\s+material)?\s*[:：]\s+$"
    r"|\btop(?:\s+material)?\s*[:：]\s+$"
)
_PART_AFTER = {
    "base": re.compile(r"^(?:[\s/-]+)?bases?\b"),
    "frame": re.compile(r"^(?:[\s/-]+)?frames?\b"),
    "seat": re.compile(r"^(?:[\s/-]+)?seats?\b"),
    "back": re.compile(r"^(?:[\s/-]+)?backs?\b"),
    "leg": re.compile(r"^(?:[\s/-]+)?legs?\b"),
    "upholstery": re.compile(r"^(?:[\s/-]+)?(?:upholstery|fabric)\b"),
}
_PART_BEFORE = {
    "base": re.compile(r"\bbases?(?:\s+material)?\s*[:：]\s+$"),
    "frame": re.compile(r"\bframes?(?:\s+material)?\s*[:：]\s+$"),
    "seat": re.compile(r"\bseats?(?:\s+material)?\s*[:：]\s+$"),
    "back": re.compile(r"\bbacks?(?:\s+material)?\s*[:：]\s+$"),
    "leg": re.compile(r"\blegs?(?:\s+material)?\s*[:：]\s+$"),
    "upholstery": re.compile(r"\b(?:upholstery|fabric)(?:\s+material)?\s*[:：]\s+$"),
}
_GENERIC_PRODUCT_AFTER = re.compile(
    r"^(?:[\s/-]+)?(?:[a-z]+\s+){0,2}(?:"
    r"cabinets?|vanit(?:y|ies)|tables?|islands?|carts?|desks?|"
    r"chairs?|sofas?|dressers?|nightstands?|bookcases?|shelv(?:es|ing)|"
    r"frames?|panels?|boards?|body|construction|countertops?|"
    r"tabletops?|desktops?|worktops?|bases?"
    r")\b"
)
_MATERIAL_VALUE_SPLIT = re.compile(r"\s*(?:,|/|\+|&|\band\b)\s*", re.I)
_MATERIAL_LABEL_KEYS = tuple(
    sorted(
        set(_STRUCTURED_MATERIAL_KEYS) | {"top", "countertop", "tabletop", "base", "frame", "seat", "back"},
        key=len,
        reverse=True,
    )
)
_FLAT_MATERIAL_KEYS = tuple(sorted(_STRUCTURED_MATERIAL_KEYS, key=len, reverse=True))
_LABELED_MATERIAL_RE = re.compile(
    r"(?i)(?<![a-z0-9])("
    + "|".join(re.escape(key) for key in _MATERIAL_LABEL_KEYS)
    + r")\s*[:：]\s*([^\n.;|]+)"
)
_FLAT_MATERIAL_KEY_RE = re.compile(
    r"(?i)(?<![a-z0-9])("
    + "|".join(re.escape(key) for key in _FLAT_MATERIAL_KEYS)
    + r")\s+([A-Za-z0-9][A-Za-z0-9+/\- ]{0,40})"
)


def is_material_aspect(name: str) -> bool:
    text = _normalize_text(name)
    if not text:
        return False
    if text == "material" or text.endswith(" material"):
        return True
    return text in _MATERIAL_ASPECT_EXACT_NAMES


def _material_aspect_part(name: str) -> str:
    text = _normalize_text(name)
    if text in {"upholstery fabric", "fabric type"}:
        return "upholstery"
    if text in {"filling", "filler", "wood type", "材质", "主材质"}:
        return "generic"
    prefix = text[: -len(" material")].strip() if text.endswith(" material") else ""
    if text == "material" or not prefix:
        return "generic"
    return _PART_PREFIX_ALIASES.get(prefix, prefix)


def _structured_key_part(key: str) -> Optional[str]:
    text = _normalize_text(key)
    if text in _STRUCTURED_MATERIAL_KEYS:
        return _STRUCTURED_MATERIAL_KEYS[text]
    if text in _PART_PREFIX_ALIASES:
        return _PART_PREFIX_ALIASES[text]
    if text.endswith(" material"):
        return _material_aspect_part(text)
    return None


def _part_accepts_field(aspect_part: str, field_part: str) -> bool:
    if aspect_part == "generic":
        return True
    return aspect_part == field_part


def _allowed_by_norm(allowed_values: Optional[Iterable[str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in allowed_values or []:
        text = str(raw or "").strip()
        if not text:
            continue
        mapping.setdefault(_normalize_text(text), text)
    return mapping


def match_allowed_whole_value(
    value: str,
    allowed_values: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Exact whole-value match after whitespace/case normalization.

    No substring or prefix matching: ``Accent`` does not match
    ``Accent Chair``. Returns the canonical allowed-list casing.
    """
    want = _normalize_text(value)
    if not want:
        return None
    allowed_map = _allowed_by_norm(allowed_values)
    return allowed_map.get(want)


def match_allowed_whole_values(
    values: Iterable[str],
    allowed_values: Optional[Iterable[str]] = None,
) -> tuple[Optional[list[str]], Optional[str]]:
    """Resolve every value against the allowed list.

    Returns ``(resolved_list, None)`` on success, or ``(None, bad_value)``
    when any entry fails whole-value equality.
    """
    resolved: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        matched = match_allowed_whole_value(text, allowed_values)
        if matched is None:
            return None, text
        resolved.append(matched)
    if not resolved:
        return None, None
    return resolved, None


def _normalize_aspect_key(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def _values_norm_list_equal(left: Iterable[str], right: Iterable[str]) -> bool:
    left_n = [_normalize_text(item) for item in left if str(item or "").strip()]
    right_n = [_normalize_text(item) for item in right if str(item or "").strip()]
    return bool(left_n) and left_n == right_n


# Counts / yes-no / room / set membership derived by the quality gate.
# Disagreeing draft values block — they do not win and are not swapped.
GATE_HARD_ASPECT_KEYS = frozenset({
    "number of pieces",
    "number of items in set",
    "assembly required",
    "room",
    "indoor/outdoor",
    "set includes",
})


def is_gate_hard_aspect(name: str) -> bool:
    return _normalize_aspect_key(name) in GATE_HARD_ASPECT_KEYS


def _mapping_by_norm_key(
    mapping: Optional[Mapping[str, Any]],
) -> dict[str, tuple[str, Any]]:
    """Map normalized aspect key -> (canonical name, raw value).

    First canonical spelling wins when duplicates differ only by case/space.
    """
    out: dict[str, tuple[str, Any]] = {}
    for name, raw in (mapping or {}).items():
        key = _normalize_aspect_key(str(name or ""))
        if not key or key in out:
            continue
        out[key] = (str(name), raw)
    return out


def _lookup_norm(
    by_norm: Mapping[str, tuple[str, Any]],
    name: str,
) -> tuple[str, Any] | tuple[None, None]:
    hit = by_norm.get(_normalize_aspect_key(name))
    if not hit:
        return None, None
    return hit


def _find_aspect_key(aspects: Mapping[str, Any], name: str) -> Optional[str]:
    want = _normalize_aspect_key(name)
    for key in aspects or {}:
        if _normalize_aspect_key(str(key)) == want:
            return str(key)
    return None


def _first_aspect_text_ci(aspects: Mapping[str, Any], name: str) -> str:
    key = _find_aspect_key(aspects, name)
    if not key:
        return ""
    return first_aspect_text(aspects, key)


def merge_aspects_for_draft_persist(
    original: Optional[Mapping[str, Any]],
    completed: Optional[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Persist auto-filled aspects only where the draft field was empty.

    Non-empty draft values are kept (preferring the completed form when the
    whole value list is only a whitespace/case normalization of the draft).
    Auto-filled keys that were missing/empty in the draft are written through.
    """
    original = original or {}
    completed = completed or {}
    completed_by_norm = _mapping_by_norm_key(completed)
    merged: dict[str, list[str]] = {}
    merged_norms: set[str] = set()

    for name, raw in original.items():
        draft_vals = _aspect_values(raw)
        if not draft_vals:
            continue
        key = _normalize_aspect_key(name)
        _canon, done_raw = _lookup_norm(completed_by_norm, name)
        done_vals = _aspect_values(done_raw)
        if done_vals and _values_norm_list_equal(done_vals, draft_vals):
            merged[str(name)] = done_vals
        else:
            merged[str(name)] = list(draft_vals)
        merged_norms.add(key)

    for name, raw in completed.items():
        key = _normalize_aspect_key(name)
        if key in merged_norms:
            continue
        done_vals = _aspect_values(raw)
        if not done_vals:
            continue
        _canon, orig_raw = _lookup_norm(_mapping_by_norm_key(original), name)
        if _aspect_values(orig_raw):
            continue
        merged[str(name)] = done_vals
        merged_norms.add(key)

    return merged


def _map_mention_to_allowed(
    mention: str,
    allowed_values: Optional[Iterable[str]],
) -> Optional[str]:
    allowed_map = _allowed_by_norm(allowed_values)
    if not allowed_map:
        return None
    mention_norm = _normalize_text(mention)
    if not mention_norm:
        return None
    if mention_norm in allowed_map:
        return allowed_map[mention_norm]
    for alias in _MATERIAL_SOURCE_ALIASES.get(mention_norm, ()):
        alias_norm = _normalize_text(alias)
        if alias_norm in allowed_map:
            return allowed_map[alias_norm]
    return None


def _split_material_value(value: str) -> list[str]:
    parts = []
    for piece in _MATERIAL_VALUE_SPLIT.split(str(value or "")):
        text = piece.strip(" -,:;.")
        if text:
            parts.append(text)
    return parts


def _value_has_hardware_context(value: str) -> bool:
    return bool(_HARDWARE_FINISH_AFTER.search(" " + _normalize_text(value)))


def _iter_mapping_material_fields(blob: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if not isinstance(blob, Mapping):
        return found
    for key, raw in blob.items():
        part = _structured_key_part(str(key or ""))
        if part is None:
            if isinstance(raw, Mapping):
                found.extend(_iter_mapping_material_fields(raw))
            continue
        for value in _aspect_values(raw):
            found.append((_normalize_text(key), value))
    return found


def _iter_labeled_material_fields(source_text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if not source_text:
        return found
    seen: set[tuple[str, str]] = set()
    for match in _LABELED_MATERIAL_RE.finditer(source_text):
        key = _normalize_text(match.group(1))
        value = match.group(2).strip(" -,:;.")
        item = (key, value)
        if key and value and item not in seen:
            seen.add(item)
            found.append(item)
    for match in _FLAT_MATERIAL_KEY_RE.finditer(source_text):
        key = _normalize_text(match.group(1))
        value = match.group(2).strip(" -,:;.")
        item = (key, value)
        if key and value and item not in seen:
            seen.add(item)
            found.append(item)
    return found


def _iter_source_material_fields(
    source_text: str,
    source_fields: Optional[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    found = _iter_mapping_material_fields(source_fields)
    found.extend(_iter_labeled_material_fields(source_text))
    return found


def _phrase_spans(phrase: str, source_norm: str) -> list[re.Match[str]]:
    if not phrase or not source_norm:
        return []
    pattern = rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])"
    return list(re.finditer(pattern, source_norm))


def _material_context_ok(
    source_norm: str,
    start: int,
    end: int,
    aspect_part: str,
    *,
    labeled_only: bool,
) -> bool:
    before = source_norm[:start]
    after = source_norm[end:]
    if _NEGATION_BEFORE.search(before):
        return False
    if _HARDWARE_FINISH_AFTER.search(after):
        return False
    labeled = bool(
        _GENERIC_MATERIAL_LABEL_BEFORE.search(before)
        or _MADE_OF_BEFORE.search(before)
        or _COUNTERTOP_BEFORE.search(before)
        or any(pattern.search(before) for pattern in _PART_BEFORE.values())
    )
    if labeled_only:
        return labeled
    if aspect_part == "generic":
        if labeled:
            return True
        return bool(_GENERIC_PRODUCT_AFTER.search(after))
    if aspect_part == "countertop":
        return bool(_COUNTERTOP_AFTER.search(after) or _COUNTERTOP_BEFORE.search(before))
    after_re = _PART_AFTER.get(aspect_part)
    before_re = _PART_BEFORE.get(aspect_part)
    if after_re and after_re.search(after):
        return True
    if before_re and before_re.search(before):
        return True
    # Unknown part ("pole material"): require that part word next to the token.
    part_word = re.escape(aspect_part)
    if re.search(rf"^(?:[\s/-]+)?{part_word}s?\b", after):
        return True
    return bool(re.search(rf"\b{part_word}s?(?:\s+material)?\s*[:：]\s+$", before))


def _candidate_material_phrases(allowed_values: Optional[Iterable[str]]) -> list[str]:
    phrases = set(_MATERIAL_SOURCE_ALIASES)
    for raw in allowed_values or []:
        text = _normalize_text(raw)
        if text:
            phrases.add(text)
    return sorted(phrases, key=len, reverse=True)


def _find_material_matching_value(
    aspect_name: str,
    source_text: str,
    allowed_values: Optional[Iterable[str]],
    source_fields: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    allowed = [str(item).strip() for item in (allowed_values or []) if str(item).strip()]
    if not allowed:
        return None
    source_text = "" if source_text is None else str(source_text)
    aspect_part = _material_aspect_part(aspect_name)

    for key, raw_value in _iter_source_material_fields(source_text, source_fields):
        field_part = _structured_key_part(key)
        if field_part is None or not _part_accepts_field(aspect_part, field_part):
            continue
        if _value_has_hardware_context(raw_value):
            continue
        mentions = _split_material_value(raw_value)
        mentions.extend(
            phrase
            for phrase in _candidate_material_phrases(allowed)
            if _phrase_spans(phrase, _normalize_text(raw_value))
        )
        seen_mentions: set[str] = set()
        for mention in mentions:
            mention_norm = _normalize_text(mention)
            if not mention_norm or mention_norm in seen_mentions:
                continue
            seen_mentions.add(mention_norm)
            if _value_has_hardware_context(mention):
                continue
            mapped = _map_mention_to_allowed(mention, allowed)
            if mapped:
                return mapped

    source_norm = _normalize_text(source_text)
    if not source_norm:
        return None
    occupied = [False] * (len(source_norm) + 1)
    for phrase in _candidate_material_phrases(allowed):
        labeled_only = phrase in _COLORISH_MATERIAL_TOKENS
        for match in _phrase_spans(phrase, source_norm):
            if any(occupied[idx] for idx in range(match.start(), match.end())):
                continue
            if not _material_context_ok(
                source_norm,
                match.start(),
                match.end(),
                aspect_part,
                labeled_only=labeled_only,
            ):
                continue
            mapped = _map_mention_to_allowed(phrase, allowed)
            if mapped:
                return mapped
            for idx in range(match.start(), match.end()):
                occupied[idx] = True
    return None


def material_value_supported_by_source(
    aspect_name: str,
    value: str,
    source_text: str,
    source_fields: Optional[Mapping[str, Any]] = None,
) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    matched = _find_material_matching_value(
        aspect_name,
        source_text,
        [text],
        source_fields,
    )
    return matched is not None


def find_source_matching_value(
    aspect_name: str,
    source_text: str,
    allowed_values: Optional[Iterable[str]] = None,
    source_fields: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    if is_material_aspect(aspect_name):
        return _find_material_matching_value(
            aspect_name,
            source_text,
            allowed_values,
            source_fields,
        )

    for candidate in allowed_values or []:
        text = str(candidate or "").strip()
        if text and value_supported_by_source(text, source_text):
            return text

    name = str(aspect_name or "").strip().lower()
    catalog: tuple[str, ...] = ()
    if name == "color":
        catalog = _COMMON_COLORS
    elif name == "type":
        catalog = _COMMON_TYPES
    for candidate in catalog:
        if value_supported_by_source(candidate, source_text):
            return candidate.title()
    return None


def restore_sourced_aspects(
    completed: MutableMapping[str, Any],
    original: Mapping[str, Any] | None,
    source_text: str,
    source_fields: Optional[Mapping[str, Any]] = None,
) -> MutableMapping[str, Any]:
    """Re-apply original optimization.aspects that are source-supported."""
    for name, raw in (original or {}).items():
        if is_skipped_aspect(name):
            if str(name or "").strip().lower() in {
                "brand",
                "mpn",
                "manufacturer part number",
            } and _aspect_values(raw):
                completed[name] = list(_aspect_values(raw))
            continue
        if is_material_aspect(name):
            values = [
                value
                for value in _aspect_values(raw)
                if material_value_supported_by_source(name, value, source_text, source_fields)
            ]
        else:
            values = [
                value
                for value in _aspect_values(raw)
                if value_supported_by_source(value, source_text)
            ]
        if values:
            completed[name] = values
    return completed


def _keep_sourced_values(
    name: str,
    values: Iterable[str],
    source_text: str,
    source_fields: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    if is_material_aspect(name):
        return [
            value
            for value in values
            if material_value_supported_by_source(name, value, source_text, source_fields)
        ]
    return [value for value in values if value_supported_by_source(value, source_text)]


def _selection_only_mode(mode: str) -> bool:
    return str(mode or "").strip().upper() == "SELECTION_ONLY"


def _mode_for_name(modes_by_norm: Mapping[str, str], name: str) -> str:
    return modes_by_norm.get(_normalize_aspect_key(name), "")


def _allowed_for_name(
    allowed_by_norm: Mapping[str, list[str]],
    name: str,
) -> list[str]:
    return list(allowed_by_norm.get(_normalize_aspect_key(name), []))


def _draft_present_keys(draft_by_norm: Mapping[str, tuple[str, Any]]) -> set[str]:
    present: set[str] = set()
    for key, (canon, raw) in draft_by_norm.items():
        if is_skipped_aspect(canon):
            continue
        if _aspect_values(raw):
            present.add(key)
    return present


def _resolve_selection_only_values(
    values: list[str],
    allowed: list[str],
) -> tuple[Optional[list[str]], Optional[str]]:
    if not allowed:
        return list(values), None
    return match_allowed_whole_values(values, allowed)


def _seed_draft_aspects(
    aspects: MutableMapping[str, Any],
    draft_aspects: Optional[Mapping[str, Any]],
    *,
    source_text: str,
    source_fields: Optional[Mapping[str, Any]],
    allowed_by_norm: Mapping[str, list[str]],
    modes_by_norm: Mapping[str, str],
    sku: str = "",
    log: Optional[LogFn] = None,
) -> tuple[set[str], set[str]]:
    """Seed seedable draft values into the working set.

    Returns ``(draft_present_norm_keys, draft_seeded_norm_keys)``.
    Gate-hard aspects are never draft-seeded. A draft value is seeded only
    when source-supported, or when every value exactly matches a
    SELECTION_ONLY allowed list and the upstream field is empty. Otherwise
    the upstream value is kept and the skip is logged.
    """
    draft_by_norm = _mapping_by_norm_key(draft_aspects)
    aspects_by_norm = _mapping_by_norm_key(aspects)
    present = _draft_present_keys(draft_by_norm)
    seeded: set[str] = set()

    for key in present:
        canon, raw = draft_by_norm[key]
        if is_gate_hard_aspect(canon):
            continue
        draft_vals = _aspect_values(raw)
        if not draft_vals:
            continue

        upstream_canon, upstream_raw = aspects_by_norm.get(key, (None, None))
        upstream_vals = _aspect_values(upstream_raw)
        working_name = upstream_canon or canon

        mode = _mode_for_name(modes_by_norm, canon)
        allowed = _allowed_for_name(allowed_by_norm, canon)
        sourced_kept = _keep_sourced_values(
            canon,
            draft_vals,
            source_text,
            source_fields,
        )
        all_sourced = bool(draft_vals) and len(sourced_kept) == len(draft_vals)

        selection_resolved: Optional[list[str]] = None
        selection_bad: Optional[str] = None
        if _selection_only_mode(mode) and allowed:
            selection_resolved, selection_bad = _resolve_selection_only_values(
                draft_vals,
                allowed,
            )

        can_seed = False
        to_seed: list[str] = []
        if all_sourced:
            if _selection_only_mode(mode) and allowed:
                if selection_resolved is None:
                    # Sourced but not on the allowed list — leave upstream;
                    # the main loop still validates if we later treat draft.
                    _emit(
                        log,
                        f"[ASPECT_DRAFT_SKIP] {sku} {working_name}="
                        f"{draft_vals[0]} not in SELECTION_ONLY "
                        f"(keeping upstream)".strip(),
                    )
                    continue
                to_seed = selection_resolved
            else:
                to_seed = list(draft_vals)
            can_seed = True
        elif selection_resolved is not None and not upstream_vals:
            to_seed = selection_resolved
            can_seed = True

        if not can_seed:
            _emit(
                log,
                f"[ASPECT_DRAFT_SKIP] {sku} {working_name}={draft_vals[0]} "
                f"unsourced draft (keeping upstream "
                f"{upstream_vals[0] if upstream_vals else 'empty'})".strip(),
            )
            continue

        target = upstream_canon or canon
        aspects[target] = list(to_seed)
        seeded.add(key)
        aspects_by_norm[key] = (target, aspects[target])

    return present, seeded


def guard_unsourced_aspects(
    aspects: MutableMapping[str, Any],
    *,
    source_text: str,
    required_names: Optional[Iterable[str]] = None,
    allowed_values_by_name: Optional[Mapping[str, Iterable[str]]] = None,
    aspect_modes_by_name: Optional[Mapping[str, str]] = None,
    expected_values_by_name: Optional[Mapping[str, Any]] = None,
    draft_aspects: Optional[Mapping[str, Any]] = None,
    source_fields: Optional[Mapping[str, Any]] = None,
    sku: str = "",
    log: Optional[LogFn] = None,
) -> list[str]:
    """Strip or block unsourced final aspects. Mutates `aspects` in place.

    Auto-fill precedence when the draft field is empty/missing:
    1. ``expected_values_by_name`` (quality-gate derived)
    2. ``find_source_matching_value`` against allowed / common catalogs
    3. leave empty; required names then block as ``unsourced_required_aspect``

    Type / descriptive draft values that were seeded beat steps 1–2.
    Gate-hard draft-vs-gate disagreements block as
    ``aspect_draft_conflicts_gate:<Name>``. SELECTION_ONLY draft/working
    values must whole-value-match every list entry or block as
    ``aspect_value_not_allowed``.
    """
    required = {
        str(name).strip()
        for name in (required_names or [])
        if str(name).strip()
    }
    required_norms = {_normalize_aspect_key(name) for name in required}
    allowed_by_norm = {
        _normalize_aspect_key(str(name)): [
            str(value) for value in values if str(value).strip()
        ]
        for name, values in (allowed_values_by_name or {}).items()
        if str(name).strip()
    }
    modes_by_norm = {
        _normalize_aspect_key(str(name)): str(mode or "").strip().upper()
        for name, mode in (aspect_modes_by_name or {}).items()
        if str(name).strip()
    }
    draft_by_norm = _mapping_by_norm_key(draft_aspects)
    draft_present, draft_seeded = _seed_draft_aspects(
        aspects,
        draft_aspects,
        source_text=source_text,
        source_fields=source_fields,
        allowed_by_norm=allowed_by_norm,
        modes_by_norm=modes_by_norm,
        sku=sku,
        log=log,
    )
    blockers: list[str] = []
    gate_conflict_norms: set[str] = set()
    not_allowed_norms: set[str] = set()

    def _expected_for(name: str) -> str:
        return _expected_value_for(name, expected_values_by_name)

    def _draft_vals_for(name: str) -> list[str]:
        _canon, raw = _lookup_norm(draft_by_norm, name)
        return _aspect_values(raw)

    def _has_blocker_for(name: str) -> bool:
        key = _normalize_aspect_key(name)
        return any(
            ":" in item
            and _normalize_aspect_key(item.split(":", 1)[-1].split("=", 1)[0]) == key
            for item in blockers
        )

    def _block_autofill(name_key: str) -> bool:
        return (
            name_key in gate_conflict_norms
            or name_key in not_allowed_norms
            or name_key in draft_seeded
            or name_key in draft_present
        )

    for name in list(aspects.keys()):
        if is_skipped_aspect(name):
            continue

        name_key = _normalize_aspect_key(name)
        values = _aspect_values(aspects.get(name))
        if values and all(is_ebay_sentinel_value(value) for value in values):
            continue

        expected = _expected_for(name)
        draft_vals = _draft_vals_for(name)
        draft_was_present = name_key in draft_present
        draft_was_seeded = name_key in draft_seeded

        if expected and is_gate_hard_aspect(name) and draft_was_present:
            if _values_match_expected(draft_vals, expected):
                aspects[name] = [expected]
                _emit(log, f"[ASPECT_GATE] {sku} {name}={expected}".strip())
                continue
            _emit(
                log,
                f"[ASPECT_BLOCK] {sku} {name} draft={draft_vals[0] if draft_vals else ''} "
                f"gate={expected} aspect_draft_conflicts_gate".strip(),
            )
            aspects.pop(name, None)
            blockers.append(f"aspect_draft_conflicts_gate:{name}")
            gate_conflict_norms.add(name_key)
            continue

        mode = _mode_for_name(modes_by_norm, name)
        allowed = _allowed_for_name(allowed_by_norm, name)
        if (
            draft_was_present
            and not is_gate_hard_aspect(name)
            and _selection_only_mode(mode)
            and allowed
            and draft_vals
        ):
            resolved, bad = _resolve_selection_only_values(draft_vals, allowed)
            if resolved is None:
                display = bad or draft_vals[0]
                _emit(
                    log,
                    f"[ASPECT_BLOCK] {sku} {name}={display} aspect_value_not_allowed".strip(),
                )
                blockers.append(f"aspect_value_not_allowed:{name}={display}")
                not_allowed_norms.add(name_key)
                # Only clear a value we ourselves seeded; never wipe a good
                # sourced upstream just because the draft was invalid.
                if draft_was_seeded:
                    aspects.pop(name, None)
                continue
            if draft_was_seeded:
                if resolved != values:
                    _emit(log, f"[ASPECT_NORM] {sku} {name}={resolved}".strip())
                aspects[name] = resolved
                values = resolved

        # Non-empty descriptive draft field: never apply expected overwrite
        # (seeded draft wins; unseedable draft keeps upstream). Still run
        # source grounding below.
        if expected and not draft_was_present:
            if _values_match_expected(values, expected):
                continue
            aspects[name] = [expected]
            _emit(log, f"[ASPECT_GATE] {sku} {name}={expected}".strip())
            continue

        if expected and draft_was_seeded and _values_match_expected(values, expected):
            continue

        kept = _keep_sourced_values(name, values, source_text, source_fields)
        power_unsupported = is_power_aspect(name) and not any(
            value_supported_by_source(marker, source_text) for marker in POWER_ASPECT_MARKERS
        )
        if power_unsupported:
            kept = []

        if kept:
            if kept != values:
                dropped = [value for value in values if value not in kept]
                for value in dropped:
                    _emit(log, f"[ASPECT_STRIP] {sku} {name}={value}".strip())
                aspects[name] = kept
            continue

        display = values[0] if values else ""
        if display:
            _emit(log, f"[ASPECT_STRIP] {sku} {name}={display}".strip())
        aspects.pop(name, None)

        if name_key not in required_norms:
            continue

        # Descriptive draft was present: never guess a replacement after strip,
        # whether or not the draft value was seedable over upstream.
        if draft_was_present and not is_gate_hard_aspect(name):
            blockers.append(f"unsourced_required_aspect:{name}")
            continue

        replacement = None if power_unsupported else find_source_matching_value(
            name,
            source_text,
            allowed,
            source_fields=source_fields,
        )
        if replacement:
            aspects[name] = [replacement]
            _emit(log, f"[ASPECT_SOURCE] {sku} {name}={replacement}".strip())
            continue
        blockers.append(f"unsourced_required_aspect:{name}")

    for name in required:
        if is_skipped_aspect(name):
            continue
        name_key = _normalize_aspect_key(name)
        if _first_aspect_text_ci(aspects, name):
            continue
        if name_key in gate_conflict_norms or name_key in not_allowed_norms:
            continue
        if name_key in draft_seeded:
            reason = f"unsourced_required_aspect:{name}"
            if not _has_blocker_for(name):
                blockers.append(reason)
            continue
        # Non-empty descriptive draft that was not seedable: do not auto-fill
        # over the empty result with expected / source guesses when the draft
        # field itself was the operator's value. Required still blocks.
        if name_key in draft_present and not is_gate_hard_aspect(name):
            reason = f"unsourced_required_aspect:{name}"
            if not _has_blocker_for(name):
                blockers.append(reason)
            continue
        expected = _expected_for(name)
        if expected:
            if name_key in draft_present and is_gate_hard_aspect(name):
                if not _has_blocker_for(name):
                    blockers.append(f"aspect_draft_conflicts_gate:{name}")
                continue
            aspects[name] = [expected]
            _emit(log, f"[ASPECT_GATE] {sku} {name}={expected}".strip())
            continue
        if is_power_aspect(name) and not any(
            value_supported_by_source(marker, source_text) for marker in POWER_ASPECT_MARKERS
        ):
            blockers.append(f"unsourced_required_aspect:{name}")
            continue
        replacement = find_source_matching_value(
            name,
            source_text,
            _allowed_for_name(allowed_by_norm, name),
            source_fields=source_fields,
        )
        if replacement:
            aspects[name] = [replacement]
            _emit(log, f"[ASPECT_SOURCE] {sku} {name}={replacement}".strip())
            continue
        reason = f"unsourced_required_aspect:{name}"
        if reason not in blockers:
            blockers.append(reason)

    for name in (expected_values_by_name or {}):
        name_key = _normalize_aspect_key(name)
        if _block_autofill(name_key):
            continue
        expected = _expected_for(name)
        if expected and not _first_aspect_text_ci(aspects, name):
            aspects[name] = [expected]
            _emit(log, f"[ASPECT_GATE] {sku} {name}={expected}".strip())

    return blockers
