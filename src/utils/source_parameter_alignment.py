"""Deterministic reconciliation of structured supplier parameters and eBay aspects.

Sergey's standing rule for Material: the live eBay value must **exactly** equal
the GIGA source string (whitespace / separator-spacing normalised only). A
family match or a first-token match is not a pass. Color still uses the older
primary-component / subset rules — that is intentional and out of scope here.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


_MATERIAL_COMPONENT_ALIASES = {
    "particleboard": "particle board",
    "mdf": "mdf",
    "engineeredwood": "engineered wood",
    "compositewood": "composite wood",
    "solidwood": "solid wood",
    "rubberwood": "rubber wood",
    "fauxleather": "faux leather",
    "pu leather": "faux leather",
}

_COLOR_COMPONENT_ALIASES = {
    "grey": "gray",
    "off white": "white",
    "cream white": "white",
    "antique white": "white",
    "walnut": "brown",
    "espresso": "brown",
    "coffee": "brown",
    "dark brown": "brown",
    "light brown": "brown",
    "antique brown": "brown",
    "sky blue": "blue",
    "khaki": "beige",
    "wood": "natural",
}

_EBAY_MAX_ASPECT_VALUE_LEN = 65

_GENERIC_MATERIAL_MEMBERS = {
    "wood": {"solid wood", "rubber wood", "oak", "pine", "teak", "walnut", "bamboo"},
    "metal": {"iron", "steel", "aluminum", "aluminium", "brass", "copper"},
    "fabric": {"chenille", "corduroy", "velvet", "polyester", "cotton", "linen"},
    "engineered wood": {"mdf", "particle board", "composite wood", "chipboard", "plywood"},
}


def _aspect_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if value not in (None, "") else []


def _source_value(attributes: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = attributes.get(key)
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                return re.sub(r"\s+", " ", parts[0]).strip()
            continue
        if value not in (None, "") and str(value).strip():
            return re.sub(r"\s+", " ", str(value)).strip()
    return ""


def _source_material_value(attributes: Mapping[str, Any]) -> str:
    """GIGA Material source: join list elements so none are dropped (B4)."""
    for key in ("Main Material", "Material", "材质"):
        value = attributes.get(key)
        if isinstance(value, (list, tuple, set)):
            parts = [re.sub(r"\s+", " ", str(item)).strip() for item in value if str(item).strip()]
            if not parts:
                continue
            # Preserve every element; ``+`` matches common GIGA composites.
            return "+".join(parts)
        if value not in (None, "") and str(value).strip():
            return re.sub(r"\s+", " ", str(value)).strip()
    return ""


def _split_components(value: Any) -> list[str]:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    if not text:
        return []
    raw = re.split(r"\s*(?:\+|,|/|&|\band\b)\s*", text)
    return [part.strip(" .;:()[]") for part in raw if part.strip(" .;:()[]")]


def primary_component(value: Any) -> str:
    """Leading component of a multi-part supplier value, preserving its casing.

    Still used for Color (out of scope for the exact-Material change). Material
    expected values must no longer go through this helper.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    parts = re.split(r"\s*(?:\+|,|/|&|\band\b)\s*", text, flags=re.IGNORECASE)
    for part in parts:
        cleaned = part.strip(" .;:()[]")
        if cleaned:
            return cleaned
    return text


def _normalize_material_component(value: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", value.lower())
    if compact in _MATERIAL_COMPONENT_ALIASES:
        return _MATERIAL_COMPONENT_ALIASES[compact]
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    return _MATERIAL_COMPONENT_ALIASES.get(normalized, normalized)


def _normalize_color_component(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    return _COLOR_COMPONENT_ALIASES.get(normalized, normalized)


def _primary_normalized(source_value: str, normalizer) -> str:
    """Normalized leading component of a possibly multi-part source value."""
    prim = primary_component(source_value)
    return normalizer(prim) if prim else ""


def normalize_material_compare_text(value: Any) -> str:
    """Casefold + collapse whitespace + drop spaces around ``+ , / &``."""
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    if not text:
        return ""
    text = re.sub(r"\s*([+,/&])\s*", r"\1", text)
    text = re.sub(r"\s+\band\b\s+", "&", text)
    return text


def _exact_material_tokens(value: Any) -> list[str]:
    """Split a material string into compare tokens without alias rewriting."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return []
    raw = re.split(r"\s*(?:\+|,|/|&|\band\b)\s*", text, flags=re.IGNORECASE)
    tokens: list[str] = []
    for part in raw:
        cleaned = part.strip(" .;:")
        if cleaned:
            tokens.append(normalize_material_compare_text(cleaned))
    return [token for token in tokens if token]


def material_exactly_matches_source(source_value: str, live_values: list[str]) -> bool:
    """Exact Material fidelity check (detection and post-fix verify).

    Passes only when:
    (a) live is a single value that equals the full GIGA string after
        case/whitespace/separator-spacing normalisation, or
    (b) live is a **multi-value** list (a split write from the GIGA value) whose
        token set equals the GIGA token set exactly. Not a superset, not a
        subset. Aliases do not count.

    Rule (b) must not apply when live is a single composite string — otherwise
    ``Steel+Steel`` vs ``["Steel"]`` or ``Metal & Wood`` vs ``["Wood/Metal"]``
    would falsely pass.
    """
    source_text = re.sub(r"\s+", " ", str(source_value or "")).strip()
    if not source_text:
        return False
    live = [str(item).strip() for item in (live_values or []) if str(item).strip()]
    if not live:
        return False

    source_norm = normalize_material_compare_text(source_text)
    if len(live) == 1:
        return normalize_material_compare_text(live[0]) == source_norm

    # Multi-value live only: compare token sets from the split write.
    source_tokens = set(_exact_material_tokens(source_text))
    live_tokens: set[str] = set()
    for value in live:
        live_tokens.update(_exact_material_tokens(value))
    return bool(source_tokens) and source_tokens == live_tokens


def _material_legacy_partial_match(source_value: str, live_values: list[str]) -> bool:
    """True when the pre-exact rules would have accepted live as faithful.

    Used to gate the scheduled bulk autofix: subset / primary / family matches
    stay report-only for Sergey; only an explicit ``--fix --fix-key Material``
    operator run writes them.
    """
    source = {
        _normalize_material_component(component)
        for component in _split_components(source_value)
    }
    live = {
        _normalize_material_component(component)
        for value in live_values
        for component in _split_components(value)
    }
    if not source or not live:
        return False
    if source <= live:
        return True
    if _primary_normalized(source_value, _normalize_material_component) in live:
        return True
    remaining = set(source)
    for generic, members in _GENERIC_MATERIAL_MEMBERS.items():
        if generic in live and members.intersection(source):
            remaining = remaining - members
    return not remaining


def _material_components_supported(source_value: str, live_values: list[str]) -> bool:
    """Backward-compatible name — Material now requires an exact match."""
    return material_exactly_matches_source(source_value, live_values)


def _color_components_supported(source_value: str, live_values: list[str]) -> bool:
    source = {
        _normalize_color_component(component)
        for component in _split_components(source_value)
    }
    live = {
        _normalize_color_component(component)
        for value in live_values
        for component in _split_components(value)
    }
    if not source or not live:
        return False
    if source <= live:
        return True
    # Single-value Color field holding the primary source color is faithful —
    # Color is intentionally unchanged by the exact-Material work.
    return _primary_normalized(source_value, _normalize_color_component) in live


def _infer_type(
    source_title: str,
    source_description: str,
    attributes: Mapping[str, Any],
    *,
    category_id: str = "",
    current_type: str = "",
) -> str:
    source_text = " ".join(
        [
            str(source_title or ""),
            str(source_description or ""),
            " ".join(str(value) for value in attributes.values()),
        ]
    ).lower()

    # A stale category default can turn a wagon stroller into a wheelbarrow
    # type.  The source title is explicit enough to repair this deterministic
    # product-family drift, and it also prevents the FactSheet guard from
    # reading the leading "2" as a wheel-count claim.
    if re.search(r"\b(?:wagon\s+stroller|stroller\s+wagon)\b", source_text):
        current_type_lower = (current_type or "").casefold()
        if not current_type_lower or "wheelbarrow" in current_type_lower or "wheeled" in current_type_lower:
            return "Wagon Stroller"

    # This guard targets the known stale category default.  A product that
    # already has a different intentional type (for example Kitchen Island)
    # must not be reclassified merely because the copy mentions a trash bin.
    if current_type and current_type.casefold() != "apothecary cabinet":
        return ""
    if re.search(r"\b(?:trash|garbage|recycling)\b", source_text) and "cabinet" in source_text:
        return "Sideboard" if str(category_id) == "183322" else "Storage Cabinet"
    if "corner cabinet" in source_text:
        return "Corner Unit" if str(category_id) == "183322" else "Corner Cabinet"
    if "wine cabinet" in source_text or "wine rack" in source_text:
        return "Sideboard" if str(category_id) == "183322" else "Wine Cabinet"
    return ""


def _aspect_cardinality(aspect_meta: Mapping[str, Any] | None) -> str:
    if not isinstance(aspect_meta, Mapping):
        return ""
    raw = (
        aspect_meta.get("cardinality")
        or aspect_meta.get("itemToAspectCardinality")
        or ""
    )
    return str(raw or "").strip().upper()


def _aspect_mode(aspect_meta: Mapping[str, Any] | None) -> str:
    if not isinstance(aspect_meta, Mapping):
        return ""
    return str(aspect_meta.get("mode") or aspect_meta.get("aspectMode") or "").strip().upper()


def _allowed_value_set(aspect_meta: Mapping[str, Any] | None) -> dict[str, str]:
    """Map casefolded allowed value -> original casing from taxonomy."""
    if not isinstance(aspect_meta, Mapping):
        return {}
    raw = aspect_meta.get("allowed_values") or []
    mapping: dict[str, str] = {}
    for item in raw:
        text = str(item or "").strip()
        if text:
            mapping.setdefault(text.casefold(), text)
    return mapping


def _source_tokens_preserving_case(source_value: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(source_value or "")).strip()
    if not text:
        return []
    parts = re.split(r"\s*(?:\+|,|/|&|\band\b)\s*", text, flags=re.IGNORECASE)
    return [part.strip(" .;:") for part in parts if part.strip(" .;:")]


def resolve_material_write(
    source_value: str,
    *,
    aspect_meta: Mapping[str, Any] | None = None,
    metadata_available: bool = True,
) -> dict[str, Any]:
    """Decide the Material write payload from live-category aspect metadata.

    Returns a dict with ``status`` of ``write`` or ``needs_review``. On write,
    ``expected`` is the list to put in ``fixes``. On review, ``reason`` explains
    why nothing may be written.
    """
    exact = re.sub(r"\s+", " ", str(source_value or "")).strip()
    mode = _aspect_mode(aspect_meta) if aspect_meta is not None else ""
    cardinality = _aspect_cardinality(aspect_meta) if aspect_meta is not None else ""

    if not metadata_available:
        return {
            "status": "needs_review",
            "expected": None,
            "reason": "aspect_metadata_unavailable",
            "aspect_mode": mode or None,
            "cardinality": cardinality or None,
        }

    # Custom aspect: name not present in the category aspect list.
    # Still enforce eBay's 65-char value limit — never hard-truncate (B3).
    if aspect_meta is None:
        if len(exact) > _EBAY_MAX_ASPECT_VALUE_LEN:
            return {
                "status": "needs_review",
                "expected": None,
                "reason": "value_too_long",
                "aspect_mode": "CUSTOM",
                "cardinality": None,
            }
        return {
            "status": "write",
            "expected": [exact],
            "reason": None,
            "aspect_mode": "CUSTOM",
            "cardinality": None,
        }

    if mode == "SELECTION_ONLY":
        allowed = _allowed_value_set(aspect_meta)
        if exact.casefold() in allowed:
            return {
                "status": "write",
                "expected": [allowed[exact.casefold()]],
                "reason": None,
                "aspect_mode": mode,
                "cardinality": cardinality or None,
            }
        if cardinality == "MULTI":
            tokens = _source_tokens_preserving_case(exact)
            if tokens and all(token.casefold() in allowed for token in tokens):
                return {
                    "status": "write",
                    "expected": [allowed[token.casefold()] for token in tokens],
                    "reason": None,
                    "aspect_mode": mode,
                    "cardinality": cardinality,
                }
        return {
            "status": "needs_review",
            "expected": None,
            "reason": "selection_only_value_not_allowed",
            "aspect_mode": mode,
            "cardinality": cardinality or None,
        }

    # FREE_TEXT (default) and any other non-selection mode: one exact value.
    # Do not split on + or &. Fail closed when over the eBay 65-char limit.
    if len(exact) > _EBAY_MAX_ASPECT_VALUE_LEN:
        return {
            "status": "needs_review",
            "expected": None,
            "reason": "value_too_long",
            "aspect_mode": mode or "FREE_TEXT",
            "cardinality": cardinality or None,
        }
    return {
        "status": "write",
        "expected": [exact],
        "reason": None,
        "aspect_mode": mode or "FREE_TEXT",
        "cardinality": cardinality or None,
    }


def find_source_parameter_mismatches(
    *,
    source_attributes: Mapping[str, Any] | None,
    source_title: str,
    source_description: str,
    candidate_aspects: Mapping[str, Any] | None,
    category_id: str = "",
    material_aspect: Mapping[str, Any] | None = None,
    aspect_metadata_available: bool = True,
) -> list[dict[str, Any]]:
    """Return explicit source-to-live aspect mismatches.

    Each item contains ``field``, ``current``, ``expected`` and ``source_key``
    so callers can both report the finding and apply a narrowly scoped fix.
    Material rows may instead be ``needs_human`` review items with no writable
    ``expected`` when taxonomy forbids a safe write.
    """
    attributes = source_attributes if isinstance(source_attributes, Mapping) else {}
    aspects = candidate_aspects if isinstance(candidate_aspects, Mapping) else {}
    mismatches: list[dict[str, Any]] = []

    source_material = _source_material_value(attributes)
    specific_upholstery = _source_value(
        attributes,
        "Upholstery Material",
        "Upholstery Fabric",
        "Fabric Type",
    )
    # GIGA sometimes emits the generic material as Leather while the more
    # specific upholstery field and variant identify PU/Faux Leather.  Prefer
    # that specific source evidence so the QC gate does not force a genuine
    # leather claim back onto the listing.
    if (
        source_material.casefold() in {"leather", "genuine leather"}
        and specific_upholstery
        and any(token in specific_upholstery.casefold() for token in ("faux", "pu"))
    ):
        source_material = specific_upholstery
    live_material = _aspect_values(aspects.get("Material"))
    if source_material and not material_exactly_matches_source(source_material, live_material):
        resolution = resolve_material_write(
            source_material,
            aspect_meta=material_aspect,
            metadata_available=aspect_metadata_available,
        )
        legacy_partial = _material_legacy_partial_match(source_material, live_material)
        detail = (
            f"Material is not source-faithful: live={live_material or 'missing'} "
            f"vs GIGA Main Material={source_material}"
        )
        if resolution["status"] == "write":
            expected_values = resolution["expected"] or []
            mismatches.append(
                {
                    "field": "Material",
                    "source_key": "Main Material",
                    "current": live_material,
                    "expected": expected_values,
                    "expected_value": expected_values[0] if len(expected_values) == 1 else expected_values,
                    "detail": detail,
                    "needs_human": False,
                    "legacy_partial_match": legacy_partial,
                    "aspect_mode": resolution.get("aspect_mode"),
                    "cardinality": resolution.get("cardinality"),
                    "category_id": str(category_id or ""),
                    "source_value": source_material,
                }
            )
        else:
            mismatches.append(
                {
                    "field": "Material",
                    "source_key": "Main Material",
                    "current": live_material,
                    "expected": None,
                    "detail": detail,
                    "needs_human": True,
                    "issue_type": "source_aspect_needs_review",
                    "reason": resolution.get("reason") or "aspect_metadata_unavailable",
                    "aspect_mode": resolution.get("aspect_mode"),
                    "cardinality": resolution.get("cardinality"),
                    "category_id": str(category_id or ""),
                    "source_value": source_material,
                    "legacy_partial_match": legacy_partial,
                }
            )

    source_color = _source_value(attributes, "Main Color", "Color", "颜色")
    live_color = _aspect_values(aspects.get("Color"))
    if source_color and not _color_components_supported(source_color, live_color):
        mismatches.append(
            {
                "field": "Color",
                "source_key": "Main Color",
                "current": live_color,
                # Single-value aspect: never hand back "Black+Gold" verbatim.
                "expected": primary_component(source_color),
                "detail": (
                    f"Color is not source-faithful: live={live_color or 'missing'} "
                    f"vs GIGA Main Color={source_color}"
                ),
            }
        )

    live_type = _aspect_values(aspects.get("Type"))
    expected_type = _infer_type(
        source_title,
        source_description,
        attributes,
        category_id=category_id,
        current_type=live_type[0] if live_type else "",
    )
    if expected_type and (not live_type or live_type[0].casefold() != expected_type.casefold()):
        mismatches.append(
            {
                "field": "Type",
                "source_key": "product family",
                "current": live_type,
                "expected": expected_type,
                "detail": (
                    f"Type is not source-faithful: live={live_type or 'missing'} "
                    f"vs inferred product family={expected_type}"
                ),
            }
        )

    return mismatches
