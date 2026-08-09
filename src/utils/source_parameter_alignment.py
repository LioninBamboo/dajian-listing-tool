"""Deterministic reconciliation of structured supplier parameters and eBay aspects.

The source snapshot is the authority for explicit material/color fields.  This
module deliberately keeps the comparison conservative: generic eBay labels are
accepted only when they preserve every source component, while a missing source
component is reported as a mismatch.
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


def _aspect_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if value not in (None, "") else []


def _source_value(attributes: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = attributes.get(key)
        if value not in (None, "") and str(value).strip():
            return re.sub(r"\s+", " ", str(value)).strip()
    return ""


def _split_components(value: Any) -> list[str]:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    if not text:
        return []
    raw = re.split(r"\s*(?:\+|,|/|&|\band\b)\s*", text)
    return [part.strip(" .;:()[]") for part in raw if part.strip(" .;:()[]")]


def _normalize_material_component(value: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", value.lower())
    if compact in _MATERIAL_COMPONENT_ALIASES:
        return _MATERIAL_COMPONENT_ALIASES[compact]
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    return _MATERIAL_COMPONENT_ALIASES.get(normalized, normalized)


def _normalize_color_component(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.lower()).strip()
    return _COLOR_COMPONENT_ALIASES.get(normalized, normalized)


def _material_components_supported(source_value: str, live_values: list[str]) -> bool:
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

    # A generic source family may be represented by its standard eBay generic
    # label, but the reverse (particle board -> wood) is intentionally not
    # accepted because it hides an engineered-wood distinction.
    generic_members = {
        "wood": {"solid wood", "rubber wood", "oak", "pine", "teak", "walnut", "bamboo"},
        "metal": {"iron", "steel", "aluminum", "aluminium", "brass", "copper"},
        "fabric": {"chenille", "corduroy", "velvet", "polyester", "cotton", "linen"},
        "engineered wood": {"mdf", "particle board", "composite wood", "chipboard", "plywood"},
    }
    for generic, members in generic_members.items():
        if generic in live and members.intersection(source):
            source = source - members
    return not source


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
    return bool(source) and bool(live) and source <= live


def _infer_type(
    source_title: str,
    source_description: str,
    attributes: Mapping[str, Any],
    *,
    category_id: str = "",
    current_type: str = "",
) -> str:
    # This guard targets the known stale category default.  A product that
    # already has a different intentional type (for example Kitchen Island)
    # must not be reclassified merely because the copy mentions a trash bin.
    if current_type and current_type.casefold() != "apothecary cabinet":
        return ""
    source_text = " ".join(
        [
            str(source_title or ""),
            str(source_description or ""),
            " ".join(str(value) for value in attributes.values()),
        ]
    ).lower()
    if re.search(r"\b(?:trash|garbage|recycling)\b", source_text) and "cabinet" in source_text:
        return "Sideboard" if str(category_id) == "183322" else "Storage Cabinet"
    if "corner cabinet" in source_text:
        return "Corner Unit" if str(category_id) == "183322" else "Corner Cabinet"
    if "wine cabinet" in source_text or "wine rack" in source_text:
        return "Sideboard" if str(category_id) == "183322" else "Wine Cabinet"
    return ""


def find_source_parameter_mismatches(
    *,
    source_attributes: Mapping[str, Any] | None,
    source_title: str,
    source_description: str,
    candidate_aspects: Mapping[str, Any] | None,
    category_id: str = "",
) -> list[dict[str, Any]]:
    """Return explicit source-to-live aspect mismatches.

    Each item contains ``field``, ``current``, ``expected`` and ``source_key``
    so callers can both report the finding and apply a narrowly scoped fix.
    """
    attributes = source_attributes if isinstance(source_attributes, Mapping) else {}
    aspects = candidate_aspects if isinstance(candidate_aspects, Mapping) else {}
    mismatches: list[dict[str, Any]] = []

    source_material = _source_value(attributes, "Main Material", "Material", "材质")
    live_material = _aspect_values(aspects.get("Material"))
    if source_material and not _material_components_supported(source_material, live_material):
        mismatches.append(
            {
                "field": "Material",
                "source_key": "Main Material",
                "current": live_material,
                "expected": source_material,
                "detail": (
                    f"Material is not source-faithful: live={live_material or 'missing'} "
                    f"vs GIGA Main Material={source_material}"
                ),
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
                "expected": source_color,
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
