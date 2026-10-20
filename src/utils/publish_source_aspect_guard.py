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


def find_source_matching_value(
    aspect_name: str,
    source_text: str,
    allowed_values: Optional[Iterable[str]] = None,
) -> Optional[str]:
    for candidate in allowed_values or []:
        text = str(candidate or "").strip()
        if text and value_supported_by_source(text, source_text):
            return text

    name = str(aspect_name or "").strip().lower()
    catalog: tuple[str, ...] = ()
    if name == "color":
        catalog = _COMMON_COLORS
    elif name == "material":
        catalog = _COMMON_MATERIALS
    elif name == "type":
        catalog = _COMMON_TYPES
    for candidate in catalog:
        if value_supported_by_source(candidate, source_text):
            return candidate.title() if name != "material" or " " in candidate else candidate.title()
    return None


def restore_sourced_aspects(
    completed: MutableMapping[str, Any],
    original: Mapping[str, Any] | None,
    source_text: str,
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
        values = [value for value in _aspect_values(raw) if value_supported_by_source(value, source_text)]
        if values:
            completed[name] = values
    return completed


def guard_unsourced_aspects(
    aspects: MutableMapping[str, Any],
    *,
    source_text: str,
    required_names: Optional[Iterable[str]] = None,
    allowed_values_by_name: Optional[Mapping[str, Iterable[str]]] = None,
    expected_values_by_name: Optional[Mapping[str, Any]] = None,
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

        kept = [value for value in values if value_supported_by_source(value, source_text)]
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
        replacement = find_source_matching_value(name, source_text, allowed_map.get(name, []))
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
