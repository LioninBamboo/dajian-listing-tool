"""Residual / fix-key matching helpers for audit emails and post-fix views."""

from __future__ import annotations

from .autofix_whitelist import SCHEDULED_SOURCE_ASPECT_AUTOFIX_ISSUE_TYPES

ACTIONABLE_SEVERITIES = frozenset({"CRITICAL", "HIGH"})

FIX_KEY_ISSUE_TYPES = {
    "categoryId": {"category_mismatch"},
    "categoryName": {"category_mismatch"},
    "__sync_video__": {"missing_video"},
    "__remove_video__": {"stale_video"},
    "__missing_foldable__": {"missing_foldable"},
    "__incomplete_title__": {"incomplete_title"},
    "__desc_needs_update__": {
        "desc_dimension_mismatch",
        "wrong_dimension",
        "wrong_weight",
    },
    "__source_parameter_rebuild__": {"source_aspect_mismatch"},
    "Assembly Required": {
        "assembly_required_mismatch",
        "assembly_package_conflict",
    },
    "__remove__Assembly Status": {"assembly_status_unsupported"},
    "__assembly_desc_update__": {
        "assembly_description_contradiction",
        "assembly_description_missing",
        "assembly_required_mismatch",
    },
    "__motors_compatibility__": {
        "missing_motors_compatibility",
        "stale_motors_compatibility",
        "motors_compatibility_metadata",
    },
}


def fix_key_matches_issue(key: str, issue_type: str) -> bool:
    """Return whether a generated fix is backed by the current issue type."""
    if key == "__semantic_rebuild_from_source__":
        return (
            issue_type.startswith("semantic_")
            or issue_type.startswith("claim_")
            or issue_type.startswith("hallucinated_")
            or issue_type == "description_raw_source_dump"
        )
    if key == "__title__":
        return issue_type == "title_cleanup"
    if key == "__rebuild_description_from_source__":
        return issue_type == "description_raw_source_dump"
    if key == "__restore_live_description_from_local__":
        return issue_type == "description_structure_missing_key_features"
    if key in {"Item Length", "Item Width", "Item Height"}:
        return issue_type in {"missing_dimension", "wrong_dimension"}
    if key == "Item Weight":
        return issue_type in {"missing_weight", "wrong_weight"}
    if key == "Material":
        return issue_type in {"source_aspect_mismatch", "semantic_material"}
    return issue_type in FIX_KEY_ISSUE_TYPES.get(key, set())


def fix_attempt_succeeded(fixes_applied) -> bool:
    texts = [str(item) for item in (fixes_applied or [])]
    if not texts:
        return False
    return not any(text.startswith("ERROR") for text in texts)


def residual_issues_for_item(item: dict) -> list[dict]:
    """CRITICAL/HIGH still needing attention after a fix attempt.

    Prefer post_fix_issues (live re-audit). Else drop issue types covered by
    selected_fix_keys / successful apply so the email does not restate
    pre-fix noise as if nothing changed.
    """
    post = item.get("post_fix_issues")
    if isinstance(post, list):
        return [
            issue
            for issue in post
            if isinstance(issue, dict)
            and str(issue.get("severity", "")).upper() in ACTIONABLE_SEVERITIES
        ]

    issues = [
        issue
        for issue in (item.get("issues") or [])
        if isinstance(issue, dict)
        and str(issue.get("severity", "")).upper() in ACTIONABLE_SEVERITIES
    ]
    if not fix_attempt_succeeded(item.get("fixes_applied")):
        return issues

    selected = {str(key) for key in (item.get("selected_fix_keys") or []) if str(key).strip()}
    if not selected:
        return issues

    residual = []
    for issue in issues:
        typ = str(issue.get("type") or "").strip()
        if any(fix_key_matches_issue(key, typ) for key in selected):
            continue
        residual.append(issue)
    return residual


def split_autofix_scope_counts(
    type_counts: dict[str, int],
) -> tuple[int, int, list[tuple[str, int]], list[tuple[str, int]]]:
    auto_parts = []
    manual_parts = []
    auto_total = 0
    manual_total = 0
    for typ, count in sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if typ in SCHEDULED_SOURCE_ASPECT_AUTOFIX_ISSUE_TYPES:
            auto_parts.append((typ, count))
            auto_total += count
        else:
            manual_parts.append((typ, count))
            manual_total += count
    return auto_total, manual_total, auto_parts[:8], manual_parts[:8]
