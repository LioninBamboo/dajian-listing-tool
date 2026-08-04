"""Shared pre-publication quality gate for generated eBay listings.

The generation, draft-audit, and publish paths must make the same decision
about a candidate listing.  This module intentionally has no eBay write
side-effects: it only normalizes/validates the candidate and runs the
FactSheet comparison supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.utils import listing_fact_sheet as _fact_sheet
from src.utils import listing_quality_gate as _quality_gate
from src.services import qc_experience_registry as _experience_registry


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


LISTING_QC_RULESET_VERSION = "listing-qc-v1"
FACT_SHEET_BLOCKING_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM"})


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
) -> dict[str, Any]:
    """Run deterministic listing QC and the semantic FactSheet guard.

    ``fact_sheet_conn`` is required for a pass.  A missing connection is
    treated as ``unavailable`` rather than silently skipping the semantic
    check.  Callers should treat every status other than ``pass`` as a
    publication blocker.
    """

    result = _base_result(sku=sku)
    candidate = dict(candidate or {})
    source_attributes = source_attributes or {}
    source_specs = source_specs or {}
    images = images or []
    videos = videos or []

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
