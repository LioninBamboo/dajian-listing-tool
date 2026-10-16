"""Per-SKU restock holds that keep eBay quantity at 0.

Daily inventory sync and ghost-OOS recovery restock sold-out listings back to
quantity 1 when the supplier still has stock. Operators can block that for a
named SKU by adding an entry to ``config/inventory_restock_holds.json``.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOLDS_PATH = PROJECT_ROOT / "config" / "inventory_restock_holds.json"


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _as_now(now: Optional[datetime]) -> datetime:
    return now if isinstance(now, datetime) else datetime.now()


def load_restock_holds(path: Optional[Path] = None) -> list[dict[str, Any]]:
    holds_path = Path(path) if path is not None else DEFAULT_HOLDS_PATH
    if not holds_path.is_file():
        return []
    try:
        payload = json.loads(holds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if isinstance(payload, dict):
        rows = payload.get("holds")
    else:
        rows = payload
    if not isinstance(rows, list):
        return []

    holds: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sku = str(row.get("sku") or "").strip()
        if not sku:
            continue
        until = _as_date(row.get("until"))
        keep_quantity = row.get("keep_quantity", 0)
        try:
            keep_quantity = int(keep_quantity)
        except (TypeError, ValueError):
            keep_quantity = 0
        holds.append({
            "sku": sku,
            "until": until,
            "keep_quantity": keep_quantity,
            "reason": str(row.get("reason") or "").strip(),
        })
    return holds


def get_active_restock_hold(
    sku: str,
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    wanted = str(sku or "").strip()
    if not wanted:
        return None
    today = _as_now(now).date()
    for hold in load_restock_holds(path=path):
        if hold["sku"] != wanted:
            continue
        until = hold.get("until")
        if until is not None and today > until:
            continue
        return hold
    return None


def is_restock_held(
    sku: str,
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    return get_active_restock_hold(sku, now=now, path=path) is not None


def should_block_positive_quantity(
    sku: str,
    quantity: Any,
    *,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> bool:
    try:
        qty = int(float(quantity))
    except (TypeError, ValueError):
        qty = 0
    return qty > 0 and is_restock_held(sku, now=now, path=path)
