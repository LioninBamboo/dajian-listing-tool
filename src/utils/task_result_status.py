"""Interpret nested task result payloads consistently for process exit status."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_FAILURE_STATUSES = {"error", "failed", "failure", "timeout", "timed_out"}
_EMPTY_FAILURE_STRINGS = {"", "0", "none", "null", "ok", "success"}


def _has_failure_payload(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _EMPTY_FAILURE_STRINGS
    if isinstance(value, Mapping):
        return any(_has_failure_payload(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return any(_has_failure_payload(item) for item in value)
    return bool(value)


def has_task_failure(result: Any) -> bool:
    """Return whether a nested task result contains a current failure signal.

    Result producers use a mixture of ``status='failed'``, ``error`` fields,
    ``*_error`` fields, and numeric ``failed`` counters. Empty error containers
    and zero failure counters are intentionally treated as success.
    """

    if isinstance(result, Mapping):
        for key, value in result.items():
            normalized = str(key).strip().lower()
            if normalized == "status" and str(value).strip().lower() in _FAILURE_STATUSES:
                return True
            if (
                normalized in {"error", "errors", "error_count", "exception", "exceptions", "failed", "failures"}
                or normalized.endswith("_error")
                or normalized.endswith("_errors")
            ) and _has_failure_payload(value):
                return True
            if has_task_failure(value):
                return True
        return False
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        return any(has_task_failure(item) for item in result)
    return False


def _is_reprice_item_level_failure(result: Any) -> bool:
    """Return True only for a completed repricing run with failed SKU rows.

    A completed batch reprice reports ``status='ok'`` and counts individual
    eBay write failures in ``summary.errors``. That must not be mistaken for a
    failure before side effects, because retrying the whole daily job would
    apply a second set of price changes.
    """

    if not isinstance(result, Mapping):
        return False
    if str(result.get("status") or "").strip().lower() not in {"ok", "success", "partial"}:
        return False
    summary = result.get("summary")
    if not isinstance(summary, Mapping):
        return False
    try:
        return int(summary.get("errors") or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_inventory_item_level_failure(result: Any) -> bool:
    """Return True for completed inventory audits with per-SKU error rows.

    The canonical contract reports ``checked_count`` and ``error_count`` for
    both the incremental sync and nested full out-of-stock audit. Legacy
    ``checked``/``errors`` aliases remain supported for older callers.
    """

    if not isinstance(result, Mapping):
        return False
    if str(result.get("status") or "").strip().lower() in _FAILURE_STATUSES:
        return False
    audits = [result]
    full_audit = result.get("full_oos_audit")
    if isinstance(full_audit, Mapping):
        audits.append(full_audit)

    saw_item_level_failure = False
    for audit in audits:
        if str(audit.get("status") or "").strip().lower() in _FAILURE_STATUSES:
            return False
        if audit.get("error"):
            return False
        try:
            checked_value = audit.get("checked_count")
            checked = int(checked_value if checked_value is not None else audit.get("checked") or 0)
            error_value = audit.get("error_count")
            errors = int(error_value if error_value is not None else audit.get("errors") or 0)
        except (TypeError, ValueError):
            return False
        if errors > 0:
            if checked <= 0:
                return False
            saw_item_level_failure = True
    return saw_item_level_failure


# Sub-task name -> recognizer for "completed run with item-level errors only".
_ITEM_LEVEL_FAILURE_RECOGNIZERS = {
    "smart_reprice": _is_reprice_item_level_failure,
    "inventory": _is_inventory_item_level_failure,
    "inventory_sync": _is_inventory_item_level_failure,
}


def _int_field(payload: Mapping[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def is_analyze_soft_degradation(payload: Any) -> bool:
    """True when analyze only skipped corrupt rows or failed to load some SKUs."""
    if not isinstance(payload, Mapping):
        return False
    if payload.get("error"):
        return False
    if _int_field(payload, "failed") > 0:
        return False
    return _int_field(payload, "skipped_corrupt") > 0 or _int_field(payload, "load_errors") > 0


def _is_reprice_time_budget_partial(result: Any) -> bool:
    """A catalog reprice that stopped on its wall-clock budget is not a failure."""
    if not isinstance(result, Mapping):
        return False
    return str(result.get("status") or "").strip().lower() == "partial"


def classify_daily_task_outcome(results: Any) -> str:
    """Classify a daily run without turning per-SKU reprice errors into reruns.

    ``partial_success`` is terminal for the scheduler: work was completed and
    external writes occurred, but a targeted retry queue is still required.
    Any other nested failure remains a normal ``failed`` result.

    ``results['analyze']`` with ``skipped_corrupt > 0`` or ``load_errors > 0``
    (and no analyze ``error``) is at least ``partial_success``. An analyze
    ``error`` remains ``failed``. A smart-reprice ``status='partial'`` time-budget
    stop is also ``partial_success`` (not silent success, not failed).
    """

    if not has_task_failure(results):
        if isinstance(results, Mapping) and (
            is_analyze_soft_degradation(results.get("analyze"))
            or _is_reprice_time_budget_partial(results.get("smart_reprice"))
        ):
            return "partial_success"
        return "success"
    if not isinstance(results, Mapping):
        return "failed"

    # Every failing sub-task must be a completed run whose only errors are
    # item-level; one genuine task failure still makes the whole run "failed".
    saw_item_level = False
    for task_name, payload in results.items():
        if not has_task_failure(payload):
            continue
        if task_name == "analyze" and is_analyze_soft_degradation(payload):
            saw_item_level = True
            continue
        recognizer = _ITEM_LEVEL_FAILURE_RECOGNIZERS.get(task_name)
        if recognizer is not None and recognizer(payload):
            saw_item_level = True
            continue
        return "failed"
    return "partial_success" if saw_item_level else "failed"


def exit_code_for_task_results(results: Any) -> int:
    """Map classify_daily_task_outcome to the process exit code (0 / 1 / 2)."""
    outcome = classify_daily_task_outcome(results)
    if outcome == "partial_success":
        return 2
    if outcome == "failed":
        return 1
    return 0
