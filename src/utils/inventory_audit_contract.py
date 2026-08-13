"""Shared result contract for the two daily inventory audit scopes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


AUDIT_SCOPE_INCREMENTAL = "incremental_inventory_sync"
AUDIT_SCOPE_FULL_OOS = "full_oos_audit"

AUDIT_SCOPE_LABELS = {
    AUDIT_SCOPE_INCREMENTAL: "已发布链接中本次增量库存同步范围",
    AUDIT_SCOPE_FULL_OOS: "全量已发布链接的 eBay 数量与供应商交叉审核",
}


def audit_scope_label(audit_scope: str | None) -> str:
    """Return a report-friendly label while preserving the machine scope value."""
    scope = str(audit_scope or "").strip()
    return AUDIT_SCOPE_LABELS.get(scope, scope or "未标明")


def _action(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("action") or "")
    return str(getattr(result, "action", "") or "")


def _sku(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("sku") or "")
    return str(getattr(result, "sku", "") or "")


def _supplier_in_stock(result: Any) -> bool | None:
    value = (
        result.get("supplier_in_stock")
        if isinstance(result, Mapping)
        else getattr(result, "supplier_in_stock", None)
    )
    return value if isinstance(value, bool) else None


def summarize_incremental_sync_results(
    results: Iterable[Any],
    *,
    scope_count: int = 0,
    skipped_count: int = 0,
) -> dict[str, Any]:
    """Convert per-SKU sync results into the stable daily-report contract.

    ``qty_zero_count`` means the sync decided to set eBay quantity to zero;
    the incremental path intentionally does not perform a second live eBay
    quantity read. The full audit owns independent live quantity verification.
    """
    rows = list(results)
    out_of_stock = [row for row in rows if _action(row) == "out_of_stock"]
    # Keep supplier shortage visible even when the subsequent eBay zero-write
    # failed; the report must show both the business state and the write error.
    supplier_oos = [row for row in rows if _supplier_in_stock(row) is False]
    restocked = [row for row in rows if _action(row) == "restocked"]
    errors = [row for row in rows if _action(row) == "error"]

    return {
        "audit_scope": AUDIT_SCOPE_INCREMENTAL,
        "checked_count": len(rows),
        "qty_zero_count": len(out_of_stock),
        "supplier_oos_count": len(supplier_oos),
        "restocked_count": len(restocked),
        "error_count": len(errors),
        "scope_count": max(0, int(scope_count or 0)),
        "skipped_count": max(0, int(skipped_count or 0)),
        "qty_zero_skus": [_sku(row) for row in out_of_stock],
        "supplier_oos_skus": [_sku(row) for row in supplier_oos],
        "restocked_items": [
            {"sku": _sku(row), "status": "已恢复"} for row in restocked
        ],
    }


def empty_inventory_audit(
    audit_scope: str,
    *,
    scope_count: int = 0,
    error_count: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    """Return a complete zero-valued audit result for error/skip paths."""
    result: dict[str, Any] = {
        "audit_scope": audit_scope,
        "checked_count": 0,
        "qty_zero_count": 0,
        "supplier_oos_count": 0,
        "restocked_count": 0,
        "error_count": max(0, int(error_count or 0)),
        "scope_count": max(0, int(scope_count or 0)),
        "skipped_count": 0,
        "qty_zero_skus": [],
        "supplier_oos_skus": [],
        "restocked_items": [],
    }
    if error:
        result["error"] = error
    return result
