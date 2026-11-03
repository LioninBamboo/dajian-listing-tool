"""Source-grounded item-specific guard used at publish time.

After template fill + placeholder sanitization, every final aspect except
dimensions/weight, Brand, and MPN is checked against the GIGA source blob
(title, description, attributes, specs). eBay sentinel values and any
aspect the listing quality gate already requires or derives are kept.
Unsourced optional claims (materials, voltage/power, warranty, …) are
stripped; unsourced required aspects are replaced with a source-matching
value when one exists, otherwise they block publish.
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


def guard_unsourced_aspects(
    aspects: MutableMapping[str, Any],
    *,
    source_text: str,
    required_names: Optional[Iterable[str]] = None,
    allowed_values_by_name: Optional[Mapping[str, Iterable[str]]] = None,
    expected_values_by_name: Optional[Mapping[str, Any]] = None,
    source_fields: Optional[Mapping[str, Any]] = None,
    sku: str = "",
    log: Optional[LogFn] = None,
) -> list[str]:
    """Strip or block unsourced final aspects. Mutates `aspects` in place."""
    required = {str(name).strip() for name in (required_names or []) if str(name).strip()}
    allowed_map = {
        str(name): [str(value) for value in values if str(value).strip()]
        for name, values in (allowed_values_by_name or {}).items()
    }
    blockers: list[str] = []

    for name in list(aspects.keys()):
        if is_skipped_aspect(name):
            continue

        values = _aspect_values(aspects.get(name))
        if values and all(is_ebay_sentinel_value(value) for value in values):
            continue

        expected = _expected_value_for(name, expected_values_by_name)
        if expected:
            if _values_match_expected(values, expected):
                continue
            aspects[name] = [expected]
            _emit(log, f"[ASPECT_GATE] {sku} {name}={expected}".strip())
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

        if name not in required:
            continue

        replacement = None if power_unsupported else find_source_matching_value(
            name,
            source_text,
            allowed_map.get(name, []),
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
        if first_aspect_text(aspects, name):
            continue
        expected = _expected_value_for(name, expected_values_by_name)
        if expected:
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
            allowed_map.get(name, []),
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
        expected = _expected_value_for(name, expected_values_by_name)
        if expected and not first_aspect_text(aspects, name):
            aspects[name] = [expected]
            _emit(log, f"[ASPECT_GATE] {sku} {name}={expected}".strip())

    return blockers
