"""Shared pre-publication quality gate for generated eBay listings.

The generation, draft-audit, and publish paths must make the same decision
about a candidate listing.  This module intentionally has no eBay write
side-effects: it only normalizes/validates the candidate and runs the
FactSheet comparison supplied by the caller.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from src.utils import listing_fact_sheet as _fact_sheet
from src.utils import listing_quality_gate as _quality_gate
from src.services import qc_experience_registry as _experience_registry
from src.services.vehicle_compatibility import (
    analyze_ebay_motors_compatibility,
    fitment_type_is_universal,
)


# Thin module-level adapters keep the service easy to monkeypatch in tests,
# while still allowing existing callers/tests that patch the source modules to
# affect this shared path.
FACT_SHEET_VERSION = _fact_sheet.FACT_SHEET_VERSION


def check_fact_sheet_violations(**kwargs):
    return _fact_sheet.check_fact_sheet_violations(**kwargs)


def validate_listing_quality(*args, **kwargs):
    return _quality_gate.validate_listing_quality(*args, **kwargs)


def blocking_issue_messages(*args, **kwargs):
    return _quality_gate.blocking_issue_messages(*args, **kwargs)


def derive_rule_ids(*, quality_issues, fact_sheet_violations):
    return _experience_registry.derive_rule_ids(
        quality_issues=quality_issues,
        fact_sheet_violations=fact_sheet_violations,
    )


def derive_contextual_rule_ids(**kwargs):
    return _experience_registry.derive_contextual_rule_ids(**kwargs)


def resolve_rule_provenance(conn, rule_ids):
    return _experience_registry.resolve_rule_provenance(conn, rule_ids)


LISTING_QC_RULESET_VERSION = "listing-qc-v2"
# MEDIUM = marketing / soft phrasing noise. Still reported as warnings for
# operators, but must NOT block conversion-focused KEY FEATURES copy.
FACT_SHEET_BLOCKING_SEVERITIES = frozenset({"CRITICAL", "HIGH"})
MOTORS_FITMENT_BLOCK_MODES = frozenset({"generic_vehicle", "needs_review"})


def resolve_qc_profile(explicit: str | None = None) -> str:
    raw = str(explicit or "").strip().lower()
    if raw:
        return raw
    try:
        from src.utils.store_profile import get_store_profile
        return str(getattr(get_store_profile(), "qc_profile", "furniture") or "furniture").strip().lower()
    except Exception:
        return "furniture"


def motors_package_includes_issues(  # motors_invented_kit
    candidate: Mapping[str, Any],
    *,
    source_title: str = "",
    source_description: str = "",
    source_attributes: Mapping[str, Any] | None = None,
    source_specs: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Set / Number of Pieces vs PACKAGE INCLUDES + invented kit size.

    Motors QC used to return after the fitment check alone. Universal-fit
    parts therefore never saw the same deterministic mismatch Main already
    flags as ``package_includes_set_qty_mismatch``. Also blocks LLM-invented
    N-piece/pack claims with no GIGA/source evidence (``invented_kit_size``).
    This does not synthesize Year/Make/Model rows.
    """
    aspects = candidate.get("aspects") if isinstance(candidate.get("aspects"), Mapping) else {}
    issues: list[Any] = []
    issue = _quality_gate.find_package_includes_set_qty_mismatch(
        str(candidate.get("title") or ""),
        str(candidate.get("description") or ""),
        aspects,
    )
    if issue is not None:
        issues.append(issue)
    issues.extend(_quality_gate.find_soft3_content_issues(
        str(candidate.get("title") or ""),
        str(candidate.get("description") or ""),
        aspects,
        f"{source_title or ''}\n{source_description or ''}",
    ))
    invented = _quality_gate.find_invented_kit_size(
        str(candidate.get("title") or ""),
        str(candidate.get("description") or ""),
        source_title=source_title,
        source_description=source_description,
        attributes=source_attributes,
        specs=source_specs,
    )
    if invented is not None:
        issues.append(invented)
    return issues


def motors_fitment_blockers(candidate: Mapping[str, Any]) -> list[str]:
    aspects = candidate.get("aspects") or {}
    analysis = analyze_ebay_motors_compatibility(
        category_id=str(candidate.get("categoryId") or ""),
        title=str(candidate.get("title") or ""),
        description=str(candidate.get("description") or ""),
        aspects=aspects,
        is_motors_store=True,
    )
    # Universal Fitment Type without a Year/Make/Model table is not a hard
    # block. Copy that mentions "trucks" must not turn that aspect into the
    # needs_review / generic_vehicle blocker that drowned daily QC.
    if not analysis.compatible_products and fitment_type_is_universal(aspects):
        return []
    if analysis.mode not in MOTORS_FITMENT_BLOCK_MODES:
        return []
    issues = [str(issue) for issue in (analysis.issues or []) if str(issue).strip()]
    if not issues:
        issues = [str(analysis.summary or "Motors fitment is incomplete")]
    return [f"[Fitment] {issue}" for issue in issues]


_COLOR_WORDS = (
    "black", "white", "red", "blue", "green", "yellow", "orange", "purple",
    "pink", "brown", "gray", "grey", "silver", "gold", "beige", "navy",
    "ivory", "burgundy", "teal", "charcoal", "khaki", "cream", "tan", "bronze",
)
_COLOR_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(sorted(_COLOR_WORDS, key=len, reverse=True)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)
_COLOR_CANONICAL = {"grey": "gray"}
_CAPACITY_RE = re.compile(
    r"\b(?P<num>\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(?P<num2>\d+(?:\.\d+)?))?\s*"
    r"(?P<unit>persons?|people|seats?|seaters?|occupants?|gallons?|gals?|"
    r"quarts?|liters?|litres?|lbs?|pounds?|kgs?|kilograms?)\b",
    re.IGNORECASE,
)
_OCCUPANCY_UNITS = {
    "person", "persons", "people", "seat", "seats", "seater", "seaters",
    "occupant", "occupants",
}
_VOLUME_UNITS = {
    "gallon", "gallons", "gal", "gals", "quart", "quarts",
    "liter", "liters", "litre", "litres",
}
_KG_UNITS = {"kg", "kgs", "kilogram", "kilograms"}
_LB_UNITS = {"lb", "lbs", "pound", "pounds"}
_LOAD_UNITS = _LB_UNITS | _KG_UNITS
_LB_PER_KG = 2.20462
# Capacity/max/limit/load/bear/hold/support/rated on either side of the
# number, same clause. "Maximum Weight" / "Max Weight" are capacity.
_CAPACITY_WORDING_RE = re.compile(
    r"\b(?:max(?:imum)?|limit|load(?:ing)?|capacit(?:y|ies)|"
    r"bears?|holds?|holding|supports?|rated)\b|"
    r"\bup\s+to\b",
    re.I,
)
_LABELLED_CAPACITY_RE = _CAPACITY_WORDING_RE
_LOAD_CONTEXT_RE = _CAPACITY_WORDING_RE
# Own-weight only when the adjacent label is an explicit weight name.
# Bare "weight" / "Maximum Weight" / "Max Weight" are not own-weight.
_EXPLICIT_WEIGHT_LABEL_RE = re.compile(
    r"(?:item|product|package|shipping|net|gross|unit|assembled|overall(?:\s+product)?)\s+weight\b|"
    r"\bweighs?\b|"
    r"(?<![a-z])weight\s*:",
    re.I,
)
_WEIGHT_CONTEXT_RE = _EXPLICIT_WEIGHT_LABEL_RE
_CLAUSE_BREAK_RE = re.compile(
    r"(?:(?<!\bmax)(?<!\boz)(?<!\bin)(?<!\bno)(?<!\bapprox)(?<!\bvs)[.!?;])\s+",
    re.I,
)
# Whole-label product/package weight keys after stripping unit parentheses.
# "Weight Capacity" / "Maximum Weight" / "Load Weight" are not weight facts.
_WEIGHT_FIELD_LABELS = frozenset({
    "weight",
    "item weight",
    "product weight",
    "net weight",
    "package weight",
    "shipping weight",
    "gross weight",
    "overall weight",
    "overall product weight",
    "unit weight",
    "assembled weight",
})
_WEIGHT_ASPECT_KEY_RE = re.compile(
    r"^(?:(?:item|product|package|shipping|net|gross|overall(?:\s+product)?|unit|assembled)\s+)?"
    r"weight"
    r"(?:\s*\([^)]*\))?$",
    re.I,
)
_WEIGHT_LABEL_UNIT_RE = re.compile(
    r"\((?P<unit>lbs?|pounds?|kgs?|kilograms?)\)|(?P<unit2>lbs?|pounds?|kgs?|kilograms?)\.?$",
    re.I,
)
_MASS_UNIT_IN_VALUE_RE = re.compile(
    r"\b(?P<unit>lbs?|pounds?|kgs?|kilograms?)\b",
    re.I,
)
_NUMERIC_MASS_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(?P<num2>\d+(?:\.\d+)?))?",
)


def _normalize_weight_label(key: object) -> str:
    text = unicodedata.normalize("NFKC", str(key or ""))
    text = re.sub(r"\s+", " ", text.strip().lower())
    text = re.sub(r"\s*\([^)]*\)\s*", " ", text)
    text = re.sub(r"\s+(?:lbs?|pounds?|kgs?|kilograms?)\.?$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def is_product_weight_label(key: object) -> bool:
    """Whole-label weight field. ``Weight Capacity`` is not a weight fact."""
    normalized = _normalize_weight_label(key)
    if not normalized or "capacity" in normalized or "limit" in normalized:
        return False
    if re.search(r"\b(?:max|maximum|load|rated)\b", normalized):
        return False
    return normalized in _WEIGHT_FIELD_LABELS


def _unit_from_weight_key(key: object) -> str:
    text = str(key or "")
    match = _WEIGHT_LABEL_UNIT_RE.search(text)
    if not match:
        return "lbs"
    unit = match.group("unit") or match.group("unit2") or "lbs"
    return "kg" if unit.lower() in _KG_UNITS else "lbs"


def _render_structured_weight(key: object, value: object) -> str | None:
    """Turn a numeric Item/Package Weight field into a unit-bearing mention."""
    if value is None:
        return None
    raw = unicodedata.normalize("NFKC", str(value)).strip()
    if not raw or raw.lower() in {"none", "null", "n/a", "na"}:
        return None
    if raw.startswith(("-", "−", "–")):
        return None
    if _MASS_UNIT_IN_VALUE_RE.search(raw) and _NUMERIC_MASS_RE.search(raw):
        try:
            number = float(_NUMERIC_MASS_RE.search(raw).group("num"))
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        return raw
    match = _NUMERIC_MASS_RE.search(raw)
    if not match:
        return None
    try:
        number = float(match.group("num"))
        number2 = float(match.group("num2")) if match.group("num2") else None
    except (TypeError, ValueError):
        return None
    if number <= 0 or (number2 is not None and number2 <= 0):
        return None
    unit = _unit_from_weight_key(key)
    token = match.group(0).replace("–", "-")
    return f"{token} {unit}"


def structured_weight_mentions(*mappings: Mapping[str, Any] | None) -> list[tuple[str, str, str]]:
    """Source item/package weight facts from structured fields, unit optional."""
    mentions: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for mapping in mappings:
        if not isinstance(mapping, Mapping):
            continue
        for key, value in mapping.items():
            if not is_product_weight_label(key):
                continue
            raw_values = value if isinstance(value, (list, tuple, set)) else [value]
            for raw in raw_values:
                rendered = _render_structured_weight(key, raw)
                if not rendered:
                    continue
                parsed = _CAPACITY_RE.search(rendered)
                if parsed is None:
                    unit = _unit_from_weight_key(key)
                    synthetic = f"{rendered} {unit}"
                    parsed = _CAPACITY_RE.search(synthetic)
                    if parsed is None:
                        continue
                    rendered = synthetic
                number = _capacity_token(parsed.group("num"), parsed.group("num2"))
                key_pair = (number, "weight")
                if key_pair in seen:
                    continue
                seen.add(key_pair)
                mentions.append((number, "weight", rendered.strip().lower()))
    return mentions


def _plain_blob(*parts: object) -> str:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, Mapping):
            for key, value in part.items():
                raw_values = value if isinstance(value, (list, tuple, set)) else [value]
                if is_product_weight_label(key):
                    for raw in raw_values:
                        rendered = _render_structured_weight(key, raw)
                        if rendered:
                            chunks.append(f"{key}: {rendered}")
                        elif raw is not None and str(raw).strip():
                            chunks.append(str(key))
                            chunks.append(str(raw))
                    continue
                chunks.append(str(key))
                for raw in raw_values:
                    if raw is not None:
                        chunks.append(str(raw))
        elif part:
            chunks.append(str(part))
    text = re.sub(r"<[^>]+>", " ", " ".join(chunks))
    return re.sub(r"\s+", " ", text).strip()


def _material_terms(text: str, structured: Mapping[str, Any] | None) -> list[str]:
    found = set(_fact_sheet.lexical_materials(text))
    for key, value in (structured or {}).items():
        if "material" not in str(key).lower():
            continue
        raw_values = value if isinstance(value, (list, tuple, set)) else [value]
        for raw in raw_values:
            for part in re.split(r"\s*(?:\+|,|/|&|\band\b)\s*", str(raw or ""), flags=re.IGNORECASE):
                cleaned = re.sub(r"\s+", " ", part).strip(" .;:()[]").lower()
                if cleaned:
                    found.add(cleaned)
    return sorted(found)


def _canonical_color(word: str) -> str:
    lowered = str(word or "").strip().lower()
    return _COLOR_CANONICAL.get(lowered, lowered)


def _color_supported(color: str, source_text: str) -> bool:
    canonical = _canonical_color(color)
    aliases = {canonical}
    for alias, canon in _COLOR_CANONICAL.items():
        if canon == canonical or alias == canonical:
            aliases.add(alias)
            aliases.add(canon)
    source = source_text.lower()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", source)
        for alias in aliases
        if alias
    )


def _colors_in_text(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _COLOR_RE.finditer(text or ""):
        canonical = _canonical_color(match.group(1))
        if canonical in seen:
            continue
        seen.add(canonical)
        found.append(canonical)
    return found


def _capacity_family(unit: str) -> str:
    lowered = str(unit or "").lower()
    if lowered in _OCCUPANCY_UNITS:
        return "occupancy"
    if lowered in _VOLUME_UNITS:
        return "volume"
    if lowered in _LOAD_UNITS:
        return "load"
    return ""


def _clause_window(text: str, start: int, end: int) -> str:
    """Same-clause window around a number. ``Max.`` is not a sentence break.

    A period after the matched unit stays in-clause (``45 lbs. capacity``)
    so the following capacity word is seen. A previous sentence ending in
    ``lbs.`` does not leak into the next mention.
    """
    blob = text or ""
    left = blob[max(0, start - 96):start]
    right = blob[end:min(len(blob), end + 64)]
    left_parts = _CLAUSE_BREAK_RE.split(left)
    left = left_parts[-1] if left_parts else left
    if right.startswith("."):
        glued = right[1:]
        glued_body = glued.lstrip()
        right_parts = _CLAUSE_BREAK_RE.split(glued_body)
        spacer = " " if glued[:1].isspace() else ""
        right = "." + spacer + (right_parts[0] if right_parts else glued_body)
    else:
        right_parts = _CLAUSE_BREAK_RE.split(right)
        right = right_parts[0] if right_parts else right
    return f"{left}{blob[start:end]}{right}"


def clause_has_capacity_wording(text: str, start: int, end: int) -> bool:
    return bool(_CAPACITY_WORDING_RE.search(_clause_window(text, start, end)))


def adjacent_explicit_weight_label(text: str, start: int, end: int) -> bool:
    """True when the label next to the number is an explicit own-weight name."""
    blob = text or ""
    left = blob[max(0, start - 40):start]
    right = blob[end:min(len(blob), end + 24)]
    return bool(_EXPLICIT_WEIGHT_LABEL_RE.search(left) or _EXPLICIT_WEIGHT_LABEL_RE.search(right))


def mass_context_family(text: str, start: int, end: int) -> str:
    """Classify an lb/kg mention from the same clause.

    Any capacity/max/limit/load/bear/hold/support/rated wording on either
    side of the number makes it a capacity claim. Own-weight only when the
    adjacent label explicitly names weight and no capacity wording is present.
    """
    blob = text or ""
    if clause_has_capacity_wording(blob, start, end):
        return "load"
    if adjacent_explicit_weight_label(blob, start, end):
        return "weight"
    return "load"


def _mass_family(text: str, match: re.Match[str]) -> str:
    return mass_context_family(text or "", match.start(), match.end())


def _capacity_number(raw: str) -> str:
    value = float(raw)
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def _capacity_token(num: str, num2: str | None = None) -> str:
    first = _capacity_number(num)
    if num2:
        return f"{first}-{_capacity_number(num2)}"
    return first


def _capacity_sort_key(token: str) -> float:
    head = str(token or "").split("-", 1)[0]
    return float(head)


def _unit_from_raw(raw: str) -> str:
    match = re.search(
        r"(persons?|people|seats?|seaters?|occupants?|gallons?|gals?|"
        r"quarts?|liters?|litres?|lbs?|pounds?|kgs?|kilograms?)\b",
        raw or "",
        re.I,
    )
    return match.group(1).lower() if match else ""


def _numeric_span(token: str) -> tuple[float, float] | None:
    text = str(token or "").strip()
    if not text:
        return None
    try:
        if "-" in text:
            left, right = text.split("-", 1)
            return float(left), float(right)
        value = float(text)
        return value, value
    except (TypeError, ValueError):
        return None


def _span_in_pounds(token: str, raw: str) -> tuple[float, float] | None:
    span = _numeric_span(token)
    if span is None:
        return None
    if _unit_from_raw(raw) in _KG_UNITS:
        return span[0] * _LB_PER_KG, span[1] * _LB_PER_KG
    return span


def _span_supported(
    claim: tuple[float, float],
    source: tuple[float, float],
    *,
    convert: bool,
) -> bool:
    c_lo, c_hi = claim
    s_lo, s_hi = source
    if convert:
        tol = max(0.6, 0.02 * max(abs(s_lo), abs(s_hi), 1.0))
    else:
        tol = 0.0
    return c_lo + 1e-9 >= s_lo - tol and c_hi - 1e-9 <= s_hi + tol


def _capacity_supported(
    number: str,
    raw: str,
    family: str,
    known: list[tuple[str, str]],
) -> bool:
    if any(number == token for token, _src_raw in known):
        return True
    # Occupancy/volume: exact token only. "6" / "5" is not sourced by "4-6".
    if family not in {"load", "weight"}:
        return False
    claim_span = _span_in_pounds(number, raw)
    if claim_span is None:
        return False
    for token, src_raw in known:
        source_span = _span_in_pounds(token, src_raw)
        if source_span is None:
            continue
        if _span_supported(claim_span, source_span, convert=True):
            return True
    return False


def capacity_window_is_labelled_load(window: str) -> bool:
    return bool(_LABELLED_CAPACITY_RE.search(window or ""))


def capacity_window_is_product_weight(window: str) -> bool:
    if capacity_window_is_labelled_load(window):
        return False
    return bool(_WEIGHT_CONTEXT_RE.search(window or ""))


def is_weight_aspect_key(key: str) -> bool:
    text = str(key or "").strip()
    if is_product_weight_label(text):
        return True
    return bool(_WEIGHT_ASPECT_KEY_RE.match(text))


def _mention_is_labelled_load(text: str, match: re.Match[str]) -> bool:
    return clause_has_capacity_wording(text or "", match.start(), match.end())


def _mention_has_explicit_weight_label(text: str, match: re.Match[str]) -> bool:
    if _mention_is_labelled_load(text, match):
        return False
    return adjacent_explicit_weight_label(text or "", match.start(), match.end())


def _capacity_mentions(text: str) -> list[tuple[str, str, str, bool]]:
    mentions: list[tuple[str, str, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    for match in _CAPACITY_RE.finditer(text or ""):
        unit = match.group("unit")
        family = _capacity_family(unit)
        if not family:
            continue
        labelled_load = False
        if family == "load":
            family = _mass_family(text or "", match)
            labelled_load = _mention_is_labelled_load(text or "", match)
        number = _capacity_token(match.group("num"), match.group("num2"))
        key = (number, family)
        if key in seen:
            continue
        seen.add(key)
        mentions.append((number, family, match.group(0).strip().lower(), labelled_load))
    return mentions


def motors_deterministic_fact_violations(
    candidate: Mapping[str, Any],
    *,
    source_title: str = "",
    source_description: str = "",
    source_attributes: Mapping[str, Any] | None = None,
    source_specs: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Material / color / capacity claims that are not in the supplier source.

    Lexicon and aspect comparison only. This does not call the EN-store LLM
    fact-sheet extractor.
    """
    aspects = candidate.get("aspects") if isinstance(candidate.get("aspects"), Mapping) else {}
    source_attributes = source_attributes or {}
    source_specs = source_specs or {}
    candidate_text = _plain_blob(
        candidate.get("title"),
        candidate.get("description"),
        aspects,
    )
    source_text = _plain_blob(
        source_title,
        source_description,
        source_attributes,
        source_specs,
    )
    source_structured = {**dict(source_attributes), **dict(source_specs)}
    violations: list[dict[str, Any]] = []

    live_materials = _material_terms(candidate_text, aspects)
    if live_materials:
        compared = _fact_sheet.compare_fact_sheets(
            {
                "materials": _material_terms(source_text, source_structured),
                "features": [],
                "counts": {},
                "capacity": None,
                "certifications": [],
                "dimensions": {},
            },
            {
                "materials": live_materials,
                "features": [],
                "counts": {},
                "capacity": None,
                "certifications": [],
                "dimensions": {},
            },
            live_text=candidate_text,
            source_text=source_text,
        )
        violations.extend(
            violation
            for violation in compared
            if str(violation.get("claim_type") or "") == "semantic_material"
        )

    for color in _colors_in_text(candidate_text):
        if _color_supported(color, source_text):
            continue
        violations.append(
            {
                "claim_type": "semantic_color",
                "claim_text": color,
                "severity": "CRITICAL",
                "source_evidence": "NOT_FOUND",
            }
        )

    source_caps = _capacity_mentions(source_text)
    source_by_family: dict[str, list[tuple[str, str]]] = {}
    for number, family, raw, _labelled in source_caps:
        source_by_family.setdefault(family, []).append((number, raw))
    for number, family, raw in structured_weight_mentions(source_attributes, source_specs):
        known_weight = source_by_family.setdefault("weight", [])
        if not any(number == token for token, _src in known_weight):
            known_weight.append((number, raw))
    source_weights = source_by_family.get("weight") or []
    source_loads = source_by_family.get("load") or []
    for number, family, raw, labelled_load in _capacity_mentions(candidate_text):
        known = source_by_family.get(family) or []
        if labelled_load:
            # Capacity wording: source load only. Never own-weight, never
            # weight conversion tolerance.
            if _capacity_supported(number, raw, "load", source_loads):
                continue
        elif family == "weight":
            if _capacity_supported(number, raw, "weight", source_weights):
                continue
        else:
            if _capacity_supported(number, raw, family, known):
                continue
            # Unlabelled bare "X lbs" may match a source weight token.
            if _capacity_supported(number, raw, "weight", source_weights):
                continue
        if known:
            evidence = ", ".join(
                dict.fromkeys(sorted((token for token, _src in known), key=_capacity_sort_key))
            )
            severity = "CRITICAL"
            claim_text = f"{raw} (source: {evidence})"
        else:
            evidence = "NOT_FOUND"
            severity = "HIGH"
            claim_text = raw
        violations.append(
            {
                "claim_type": "semantic_capacity",
                "claim_text": claim_text,
                "severity": severity,
                "source_evidence": evidence,
            }
        )
    return violations


def motors_claim_invent_issues(
    candidate: Mapping[str, Any],
    *,
    source_title: str = "",
    source_description: str = "",
    source_attributes: Mapping[str, Any] | None = None,
    source_specs: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic claim-diff invents (features, upgrades, certifications)."""
    from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations

    aspects = candidate.get("aspects") if isinstance(candidate.get("aspects"), Mapping) else {}
    constraints = build_source_constraints(
        attrs=source_attributes or {},
        specs=source_specs or {},
        source_description=source_description,
        source_title=source_title,
    )
    issues: list[dict[str, Any]] = []
    for violation in detect_claim_violations(
        source_constraints=constraints,
        generated_title=str(candidate.get("title") or ""),
        generated_description=str(candidate.get("description") or ""),
        generated_aspects=dict(aspects),
    ):
        severity = str(violation.severity or "").upper()
        if severity not in FACT_SHEET_BLOCKING_SEVERITIES:
            continue
        issues.append(
            {
                "code": f"claim_{violation.claim_type}",
                "message": (
                    f"[Claim] {violation.claim_type} ({severity}): {violation.claim_text}"
                    f" (source: {violation.source_evidence})"
                ),
                "severity": severity,
                "field": violation.location,
            }
        )
    return issues


def _run_motors_listing_qc(
    *,
    sku: str,
    candidate: Mapping[str, Any],
    source_title: str,
    source_description: str,
    source_attributes: Mapping[str, Any],
    source_specs: Mapping[str, Any],
) -> dict[str, Any]:
    """Fitment plus deterministic invent gates.

    Furniture ``validate_listing_quality`` and the LLM FactSheet extractor stay
    off this path (the extractor is optional and costly on the EN store). The
    deterministic material / color / capacity diff and claim-invent gate still
    run, including when fitment itself is empty or universal.
    """
    result = _base_result(sku=sku)
    package_issues = motors_package_includes_issues(
        candidate,
        source_title=source_title,
        source_description=source_description,
        source_attributes=source_attributes,
        source_specs=source_specs,
    )
    fitment_blockers = motors_fitment_blockers(candidate)
    gate_errors: list[str] = []
    try:
        fact_violations = motors_deterministic_fact_violations(
            candidate,
            source_title=source_title,
            source_description=source_description,
            source_attributes=source_attributes,
            source_specs=source_specs,
        )
    except Exception as exc:
        fact_violations = []
        gate_errors.append(f"FactSheet guard error: {exc}")
    try:
        claim_issues = motors_claim_invent_issues(
            candidate,
            source_title=source_title,
            source_description=source_description,
            source_attributes=source_attributes,
            source_specs=source_specs,
        )
    except Exception as exc:
        claim_issues = []
        gate_errors.append(f"Claim invent gate error: {exc}")

    result["quality_issues"] = [
        _serialize_quality_issue(issue) for issue in [*package_issues, *claim_issues]
    ]
    result["fact_sheet_violations"] = [
        _serialize_fact_violation(violation) for violation in fact_violations
    ]
    if gate_errors and not fact_violations:
        result["fact_sheet_status"] = "unavailable"
    elif fact_violations:
        result["fact_sheet_status"] = "violations"
    else:
        result["fact_sheet_status"] = "pass"

    blockers = list(fitment_blockers)
    blockers.extend(str(getattr(issue, "message", issue)) for issue in package_issues)
    for violation in result["fact_sheet_violations"]:
        severity = str(violation.get("severity") or "UNKNOWN").upper()
        message = _fact_sheet_message(violation)
        if severity in FACT_SHEET_BLOCKING_SEVERITIES:
            blockers.append(message)
        else:
            result["warnings"].append(message)
    blockers.extend(
        str(issue["message"])
        for issue in result["quality_issues"]
        if str(issue.get("code") or "").startswith("claim_")
    )
    blockers.extend(gate_errors)
    result["blockers"] = _dedupe(blockers)
    result["warnings"] = _dedupe(result["warnings"])
    result["rule_ids"] = derive_rule_ids(
        quality_issues=result["quality_issues"],
        fact_sheet_violations=result["fact_sheet_violations"],
    )
    only_gate_error = (
        result["fact_sheet_status"] == "unavailable"
        and not fitment_blockers
        and not package_issues
        and not fact_violations
        and not claim_issues
    )
    if result["blockers"]:
        result["status"] = "unavailable" if only_gate_error else "blocked"
    else:
        result["status"] = "pass"
    return result


def _dedupe(messages: list[str]) -> list[str]:
    return list(dict.fromkeys(str(message) for message in messages if str(message).strip()))


def _serialize_quality_issue(issue: Any) -> dict[str, Any]:
    if isinstance(issue, Mapping):
        return {
            "code": str(issue.get("code") or "quality_issue"),
            "message": str(issue.get("message") or ""),
            "severity": str(issue.get("severity") or "BLOCKER"),
            "field": issue.get("field"),
        }
    return {
        "code": str(getattr(issue, "code", "quality_issue")),
        "message": str(getattr(issue, "message", issue)),
        "severity": str(getattr(issue, "severity", "BLOCKER")),
        "field": getattr(issue, "field", None),
    }


def _serialize_fact_violation(violation: Any) -> dict[str, Any]:
    if isinstance(violation, Mapping):
        return dict(violation)
    return {"claim_text": str(violation)}


def _mass_token_from_claim(text: object) -> str | None:
    match = _CAPACITY_RE.search(str(text or ""))
    if match is None:
        return None
    if _capacity_family(match.group("unit")) != "load":
        return None
    return _capacity_token(match.group("num"), match.group("num2"))


def explicit_supported_own_weight_tokens(
    candidate: Mapping[str, Any],
    *,
    source_title: str = "",
    source_description: str = "",
    source_attributes: Mapping[str, Any] | None = None,
    source_specs: Mapping[str, Any] | None = None,
) -> set[str]:
    """lb/kg tokens det classified as sourced own-weight with an explicit label."""
    aspects = candidate.get("aspects") if isinstance(candidate.get("aspects"), Mapping) else {}
    candidate_text = _plain_blob(
        candidate.get("title"),
        candidate.get("description"),
        aspects,
    )
    source_text = _plain_blob(
        source_title,
        source_description,
        source_attributes or {},
        source_specs or {},
    )
    source_weights: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, family, raw, labelled_load in _capacity_mentions(source_text):
        if family != "weight" or labelled_load:
            continue
        if number in seen:
            continue
        seen.add(number)
        source_weights.append((number, raw))
    for number, _family, raw in structured_weight_mentions(source_attributes, source_specs):
        if number in seen:
            continue
        seen.add(number)
        source_weights.append((number, raw))
    tokens: set[str] = set()
    for match in _CAPACITY_RE.finditer(candidate_text):
        if _capacity_family(match.group("unit")) != "load":
            continue
        if not _mention_has_explicit_weight_label(candidate_text, match):
            continue
        number = _capacity_token(match.group("num"), match.group("num2"))
        raw = match.group(0).strip().lower()
        if _capacity_supported(number, raw, "weight", source_weights):
            tokens.add(number)
    return tokens


def merge_deterministic_fact_violations(
    violations: list[Any],
    candidate: Mapping[str, Any],
    *,
    source_title: str = "",
    source_description: str = "",
    source_attributes: Mapping[str, Any] | None = None,
    source_specs: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Union LLM FactSheet hits with the deterministic invent gate.

    Keep an LLM ``semantic_capacity`` lb/kg flag unless det positively
    classified that exact figure as a supported own-weight fact with an
    explicit weight label. Occupancy / volume stay strict.
    """
    source_attributes = source_attributes or {}
    source_specs = source_specs or {}
    det = [
        _serialize_fact_violation(item)
        for item in motors_deterministic_fact_violations(
            candidate,
            source_title=source_title,
            source_description=source_description,
            source_attributes=source_attributes,
            source_specs=source_specs,
        )
    ]
    supported_own = explicit_supported_own_weight_tokens(
        candidate,
        source_title=source_title,
        source_description=source_description,
        source_attributes=source_attributes,
        source_specs=source_specs,
    )
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def _add(item: Mapping[str, Any]) -> None:
        payload = _serialize_fact_violation(item)
        key = (
            str(payload.get("claim_type") or ""),
            re.sub(r"\s+", " ", str(payload.get("claim_text") or "")).strip().lower(),
        )
        if not key[0] or key in seen:
            return
        seen.add(key)
        merged.append(payload)

    for item in violations or []:
        payload = _serialize_fact_violation(item)
        if str(payload.get("claim_type") or "") == "semantic_capacity":
            token = _mass_token_from_claim(payload.get("claim_text"))
            if token is not None and token in supported_own:
                continue
        _add(payload)
    for item in det:
        _add(item)
    return merged


def _fact_sheet_message(violation: Mapping[str, Any]) -> str:
    claim_type = str(violation.get("claim_type") or "unknown")
    severity = str(violation.get("severity") or "UNKNOWN")
    claim_text = str(violation.get("claim_text") or "")
    source_evidence = str(violation.get("source_evidence") or "")
    message = f"[FactSheet] {claim_type} ({severity}): {claim_text}"
    if source_evidence:
        message += f" (source: {source_evidence})"
    return message


def _base_result(*, sku: str, fact_sheet_status: str = "not_run") -> dict[str, Any]:
    return {
        "sku": str(sku or ""),
        "status": "blocked",
        "quality_issues": [],
        "fact_sheet_status": fact_sheet_status,
        "fact_sheet_violations": [],
        "blockers": [],
        "warnings": [],
        "source_fingerprint": "",
        "candidate_fingerprint": "",
        "ruleset_version": LISTING_QC_RULESET_VERSION,
        "fact_sheet_version": FACT_SHEET_VERSION,
        "rule_ids": [],
        "experience_ids": [],
        "rule_provenance": [],
        "evidence_refs": [],
    }


def run_listing_qc(
    *,
    sku: str = "",
    candidate: Mapping[str, Any] | None,
    source_title: str = "",
    source_description: str = "",
    source_attributes: Mapping[str, Any] | None = None,
    source_specs: Mapping[str, Any] | None = None,
    images: list[str] | None = None,
    videos: list[str] | None = None,
    category_matcher: Any = None,
    fact_sheet_conn: Any = None,
    qc_profile: str | None = None,
) -> dict[str, Any]:
    """Run deterministic listing QC and the semantic FactSheet guard.

    ``fact_sheet_conn`` is required for a furniture pass.  A missing connection
    is treated as ``unavailable`` rather than silently skipping the semantic
    check.  Callers should treat every status other than ``pass`` as a
    publication blocker.

    ``qc_profile=motors`` still blocks incomplete vehicle-specific fitment and
    package-includes quantity mismatches. It also runs the deterministic
    material / color / capacity invent diff and the claim-invent gate, including
    for universal-fit parts. The EN-store LLM FactSheet extractor stays off
    this path. Universal Fitment Type with no Year/Make/Model table is not a
    fitment hard block.
    """

    result = _base_result(sku=sku)
    candidate = dict(candidate or {})
    source_attributes = source_attributes or {}
    source_specs = source_specs or {}
    images = images or []
    videos = videos or []
    profile = resolve_qc_profile(qc_profile)

    if profile == "motors":
        return _run_motors_listing_qc(
            sku=sku,
            candidate=candidate,
            source_title=source_title,
            source_description=source_description,
            source_attributes=source_attributes,
            source_specs=source_specs,
        )

    try:
        quality_issues_raw = validate_listing_quality(
            candidate,
            source_title=source_title,
            source_description=source_description,
            attributes=source_attributes,
            specs=source_specs,
            images=images,
            videos=videos,
            category_matcher=category_matcher,
            listing_status=str(candidate.get("listingStatus") or candidate.get("status") or ""),
        )
    except Exception as exc:
        result["blockers"] = [f"Quality gate error: {exc}"]
        result["error"] = str(exc)
        return result

    quality_issues = [_serialize_quality_issue(issue) for issue in (quality_issues_raw or [])]
    result["quality_issues"] = quality_issues
    result["blockers"] = _dedupe(
        blocking_issue_messages(quality_issues_raw or [])
    )
    result["warnings"] = _dedupe(
        [issue["message"] for issue in quality_issues if issue["severity"] != "BLOCKER"]
    )

    if fact_sheet_conn is None:
        fact_result: dict[str, Any] = {
            "status": "unavailable",
            "violations": [],
            "source_fingerprint": "",
            "candidate_fingerprint": "",
        }
    else:
        try:
            fact_result = check_fact_sheet_violations(
                conn=fact_sheet_conn,
                source_title=source_title,
                source_description=source_description,
                source_attributes=source_attributes,
                source_specs=source_specs,
                candidate_title=candidate.get("title") or source_title,
                candidate_description=candidate.get("description") or source_description,
                candidate_aspects=candidate.get("aspects") or {},
            ) or {}
        except Exception as exc:
            fact_result = {
                "status": "unavailable",
                "violations": [],
                "source_fingerprint": "",
                "candidate_fingerprint": "",
                "error": str(exc),
            }

    fact_violations = merge_deterministic_fact_violations(
        list(fact_result.get("violations") or []),
        candidate,
        source_title=source_title,
        source_description=source_description,
        source_attributes=source_attributes,
        source_specs=source_specs,
    )
    fact_status = str(fact_result.get("status") or "unavailable").lower()
    if fact_violations and fact_status == "pass":
        fact_status = "violations"
    result["fact_sheet_status"] = fact_status
    result["fact_sheet_violations"] = fact_violations
    result["source_fingerprint"] = str(fact_result.get("source_fingerprint") or "")
    result["candidate_fingerprint"] = str(fact_result.get("candidate_fingerprint") or "")

    result["rule_ids"] = derive_rule_ids(
        quality_issues=result["quality_issues"],
        fact_sheet_violations=result["fact_sheet_violations"],
    )
    result["rule_ids"] = list(
        dict.fromkeys(
            result["rule_ids"]
            + derive_contextual_rule_ids(
                source_title=source_title,
                candidate_title=candidate.get("title") or source_title,
                quality_issues=result["quality_issues"],
                fact_sheet_violations=result["fact_sheet_violations"],
            )
        )
    )
    if fact_sheet_conn is not None and result["rule_ids"]:
        try:
            result["rule_provenance"] = resolve_rule_provenance(
                fact_sheet_conn,
                result["rule_ids"],
            )
        except Exception:
            # Provenance lookup must never turn a valid QC decision into a
            # false pass or a runtime failure. Unknown rules remain visible in
            # rule_ids and can be registered later.
            result["rule_provenance"] = []
    result["experience_ids"] = _dedupe(
        [item["experience_id"] for item in result["rule_provenance"] if item.get("experience_id")]
    )
    evidence_refs = []
    for issue in result["quality_issues"]:
        issue_rule_ids = derive_rule_ids(quality_issues=[issue], fact_sheet_violations=[])
        if issue_rule_ids:
            evidence_refs.append(
                {
                    "rule_id": issue_rule_ids[0],
                    "kind": "quality_issue",
                    "code": issue.get("code"),
                    "message": issue.get("message"),
                    "field": issue.get("field"),
                }
            )
    for violation in result["fact_sheet_violations"]:
        violation_rule_ids = derive_rule_ids(quality_issues=[], fact_sheet_violations=[violation])
        if violation_rule_ids:
            evidence_refs.append(
                {
                    "rule_id": violation_rule_ids[0],
                    "kind": "fact_sheet",
                    "claim_type": violation.get("claim_type"),
                    "claim_text": violation.get("claim_text"),
                    "source_evidence": violation.get("source_evidence"),
                }
            )
    result["evidence_refs"] = evidence_refs

    if fact_status == "unavailable":
        if fact_result.get("error"):
            result["blockers"].append(f"FactSheet guard error: {fact_result['error']}")
        else:
            result["blockers"].append("FactSheet guard unavailable (LLM failed or disabled)")
    elif fact_status != "pass" and fact_status != "violations":
        result["blockers"].append(f"FactSheet guard returned invalid status: {fact_status}")
    for violation in result["fact_sheet_violations"]:
        severity = str(violation.get("severity") or "UNKNOWN").upper()
        message = _fact_sheet_message(violation)
        if severity in FACT_SHEET_BLOCKING_SEVERITIES:
            result["blockers"].append(message)
        else:
            result["warnings"].append(message)

    result["blockers"] = _dedupe(result["blockers"])
    result["warnings"] = _dedupe(result["warnings"])

    if result["blockers"]:
        if (
            fact_status == "unavailable"
            and not result["quality_issues"]
            and not result["fact_sheet_violations"]
        ):
            result["status"] = "unavailable"
        else:
            result["status"] = "blocked"
    else:
        result["status"] = "pass"

    return result
