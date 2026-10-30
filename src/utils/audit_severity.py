"""Canonical severities for listing-audit issue dicts.

Non-standard labels (WARNING, INFO, BLOCKER, …) used to reach
``daily_tasks.run_listing_audit`` and make ``min()`` raise on an empty
iterable. Every audit issue is mapped onto CRITICAL / HIGH / MEDIUM / LOW
before it is ranked. Report-only category findings stay report-only after
that mapping: they must not mark a listing dirty, trigger a fix, or write
the row.
"""

from __future__ import annotations

CANONICAL = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

_ALIASES = {
    "CRITICAL": "CRITICAL",
    "BLOCKER": "CRITICAL",
    "HIGH": "HIGH",
    "ERROR": "HIGH",
    "MEDIUM": "MEDIUM",
    "WARNING": "MEDIUM",
    "WARN": "MEDIUM",
    "LOW": "LOW",
    "INFO": "LOW",
}

_REPORT_ONLY_TYPES: frozenset[str] | None = None


def normalize_severity(value) -> str:
    """Map any severity label onto ``CANONICAL``. Never raises.

    Case-insensitive. ``BLOCKER→CRITICAL``, ``ERROR→HIGH``,
    ``WARNING``/``WARN→MEDIUM``, ``INFO→LOW``. Unknown, None, or blank → LOW.
    """
    if value is None:
        return "LOW"
    try:
        text = str(value).strip()
    except Exception:
        return "LOW"
    if not text:
        return "LOW"
    return _ALIASES.get(text.upper(), "LOW")


def report_only_issue_types() -> frozenset[str]:
    """Issue types that stay report-only after severity normalization."""
    global _REPORT_ONLY_TYPES
    if _REPORT_ONLY_TYPES is None:
        from src.utils.approved_category import PIN_MISMATCH_REPORT
        from src.utils.category_decision import NEEDS_MANUAL_MOTORS_LEAF_REVIEW

        _REPORT_ONLY_TYPES = frozenset(
            {
                "title_source_conflict",
                "category_remap_pending_review",
                PIN_MISMATCH_REPORT,
                NEEDS_MANUAL_MOTORS_LEAF_REVIEW,
            }
        )
    return _REPORT_ONLY_TYPES


def is_report_only_issue(issue) -> bool:
    """True when an issue must not dirty a listing, trigger a fix, or be written.

    The explicit ``report_only`` flag wins. The known category/gate types are
    also report-only so a pre-normalization payload (severity still WARNING or
    INFO, flag not yet stamped) cannot start writing once its severity is
    normalized.
    """
    if not isinstance(issue, dict):
        return False
    if issue.get("report_only") is True:
        return True
    return str(issue.get("type") or "").strip() in report_only_issue_types()
