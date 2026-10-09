"""Shared pre-publication quality gate for generated eBay listings.

The generation, draft-audit, and publish paths must make the same decision
about a candidate listing.  This module intentionally has no eBay write
side-effects: it only normalizes/validates the candidate and runs the
FactSheet comparison supplied by the caller.
"""

from __future__ import annotations

import re
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


def motors_package_includes_issues(candidate: Mapping[str, Any]) -> list[Any]:
    """Set / Number of Pieces vs PACKAGE INCLUDES. Independent of fitment.

    Motors QC used to return after the fitment check alone. Universal-fit
    parts therefore never saw the same deterministic mismatch Main already
    flags as ``package_includes_set_qty_mismatch``. This does not synthesize
    Year/Make/Model rows.
    """
    aspects = candidate.get("aspects") if isinstance(candidate.get("aspects"), Mapping) else {}
    issue = _quality_gate.find_package_includes_set_qty_mismatch(
        str(candidate.get("title") or ""),
        str(candidate.get("description") or ""),
        aspects,
    )
    return [issue] if issue is not None else []


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
# Keep both ends of "2-4" / "2 to 4". A candidate span is supported only when
# it sits inside a source span of the same family, so "4 persons" matches
# source "2-4 persons" and "2-5 gallons" does not match source "2 gallon".
# seat/seats is a product noun on motors copy ("2 Seat Cover"), not occupancy;
# seater/people/occupant still count. person/persons skips install copy
# ("1 person assembly", "1 person installation").
_CAPACITY_RE = re.compile(
    r"\b(?P<num>\d+(?:\.\d+)?)"
    r"(?:\s*(?:-|–|to)\s*(?P<high>\d+(?:\.\d+)?))?"
    r"\s*"
    r"(?P<unit>"
    r"people|seaters?|occupants?|"
    r"persons?(?![\s-]+(?:assembly|install(?:ation)?)\b)|"
    r"gallons?|gals?|quarts?|liters?|litres?"
    r")\b",
    re.IGNORECASE,
)
_OCCUPANCY_UNITS = {
    "person", "persons", "people", "seater", "seaters",
    "occupant", "occupants",
}
_VOLUME_UNITS = {
    "gallon", "gallons", "gal", "gals", "quart", "quarts",
    "liter", "liters", "litre", "litres",
}


def _plain_blob(*parts: object) -> str:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, Mapping):
            for key, value in part.items():
                chunks.append(str(key))
                if isinstance(value, (list, tuple, set)):
                    chunks.extend(str(item) for item in value)
                elif value is not None:
                    chunks.append(str(value))
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
    return ""


def _capacity_number(raw: float | str) -> str:
    value = float(raw)
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def _format_capacity_span(low: float, high: float) -> str:
    low_text = _capacity_number(low)
    if low == high:
        return low_text
    return f"{low_text}-{_capacity_number(high)}"


def _capacity_span_supported(
    low: float,
    high: float,
    known: list[tuple[float, float]],
) -> bool:
    return any(src_low <= low and high <= src_high for src_low, src_high in known)


def _capacity_mentions(text: str) -> list[tuple[float, float, str, str]]:
    mentions: list[tuple[float, float, str, str]] = []
    seen: set[tuple[float, float, str]] = set()
    for match in _CAPACITY_RE.finditer(text or ""):
        family = _capacity_family(match.group("unit"))
        if not family:
            continue
        low = float(match.group("num"))
        high_raw = match.group("high")
        high = float(high_raw) if high_raw else low
        if high < low:
            low, high = high, low
        key = (low, high, family)
        if key in seen:
            continue
        seen.add(key)
        mentions.append((low, high, family, match.group(0).strip().lower()))
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
    source_by_family: dict[str, list[tuple[float, float]]] = {}
    for low, high, family, _raw in source_caps:
        source_by_family.setdefault(family, []).append((low, high))
    for low, high, family, raw in _capacity_mentions(candidate_text):
        known = source_by_family.get(family) or []
        if _capacity_span_supported(low, high, known):
            continue
        if known:
            evidence = ", ".join(
                _format_capacity_span(src_low, src_high)
                for src_low, src_high in sorted(known)
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
    package_issues = motors_package_includes_issues(candidate)
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

    fact_status = str(fact_result.get("status") or "unavailable").lower()
    result["fact_sheet_status"] = fact_status
    result["fact_sheet_violations"] = [
        _serialize_fact_violation(violation)
        for violation in (fact_result.get("violations") or [])
    ]
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
    elif fact_status == "violations":
        for violation in result["fact_sheet_violations"]:
            severity = str(violation.get("severity") or "UNKNOWN").upper()
            message = _fact_sheet_message(violation)
            if severity in FACT_SHEET_BLOCKING_SEVERITIES:
                result["blockers"].append(message)
            else:
                result["warnings"].append(message)
    elif fact_status != "pass":
        result["blockers"].append(f"FactSheet guard returned invalid status: {fact_status}")

    result["blockers"] = _dedupe(result["blockers"])
    result["warnings"] = _dedupe(result["warnings"])

    if result["blockers"]:
        if fact_status == "unavailable" and not result["quality_issues"]:
            result["status"] = "unavailable"
        else:
            result["status"] = "blocked"
    else:
        result["status"] = "pass"

    return result
