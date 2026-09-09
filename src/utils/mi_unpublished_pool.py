"""Refill the MI unpublished pool from local ENDED rows.

ENDED means the eBay listing is gone. Those SKUs still have source data and
can re-enter PENDING so daily MI can score and auto-prepare them.

DELISTED is left alone: that status is used for operator/CRO/policy takedowns
and often already has an MI blacklist TTL.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.utils.mi_opportunity_flow import _store_kind_allows

_COST_KEYS = (
    "total_dajian_cost",
    "landed_cost",
    "total_cost",
    "base_cost",
    "product_price",
)
_RECYCLE_STATUSES = ("ENDED",)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _positive_cost(raw: Any) -> float:
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        return 0.0
    for key in _COST_KEYS:
        try:
            value = float(data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _image_count(raw: Any) -> int:
    try:
        images = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        images = []
    if not isinstance(images, list):
        return 0
    return len([item for item in images if str(item or "").strip()])


def recycle_ended_for_mi(
    db_path: str,
    *,
    limit: int = 20,
    store_kind: str = "furniture",
    blacklist: set[str] | None = None,
    stock_lookup: Callable[[str], int | None] | None = None,
) -> dict[str, Any]:
    """Flip eligible ENDED rows to PENDING and clear stale listing_id."""
    skipped: Counter[str] = Counter()
    reactivated: list[str] = []
    cap = max(0, int(limit or 0))
    blocked = {str(sku).strip() for sku in (blacklist or set()) if str(sku).strip()}

    if cap <= 0 or not Path(db_path).exists():
        return {"reactivated": [], "skipped": {}, "requested": 0}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in _RECYCLE_STATUSES)
        rows = conn.execute(
            f"""
            SELECT sku, title, status, listing_id, cost_breakdown, images, logs
            FROM collected_products
            WHERE status IN ({placeholders})
            ORDER BY COALESCE(updated_at, '') DESC, sku ASC
            """,
            _RECYCLE_STATUSES,
        ).fetchall()

        now = _utcnow()
        for row in rows:
            if len(reactivated) >= cap:
                break
            sku = str(row["sku"] or "").strip()
            title = str(row["title"] or "")
            if not sku:
                skipped["invalid"] += 1
                continue
            if sku in blocked:
                skipped["blacklist"] += 1
                continue
            if _positive_cost(row["cost_breakdown"]) <= 0:
                skipped["zero_cost"] += 1
                continue
            if _image_count(row["images"]) < 2:
                skipped["too_few_images"] += 1
                continue
            if not _store_kind_allows(title, store_kind):
                skipped["auto_family"] += 1
                continue
            if stock_lookup is not None:
                try:
                    quantity = stock_lookup(sku)
                except Exception:
                    quantity = None
                if quantity is not None and int(quantity) <= 0:
                    skipped["zero_stock"] += 1
                    continue
            try:
                logs = json.loads(row["logs"]) if row["logs"] else []
                if not isinstance(logs, list):
                    logs = [str(logs)]
            except (TypeError, ValueError, json.JSONDecodeError):
                logs = []
            logs.append(f"[MI_ENDED_RECYCLE] Restored ENDED SKU to PENDING at {now}")
            conn.execute(
                """
                UPDATE collected_products
                SET status = 'PENDING', listing_id = NULL, logs = ?, updated_at = ?
                WHERE sku = ? AND status = 'ENDED'
                """,
                (json.dumps(logs, ensure_ascii=False), now, sku),
            )
            reactivated.append(sku)
        conn.commit()
    finally:
        conn.close()

    return {
        "reactivated": reactivated,
        "skipped": dict(skipped),
        "requested": cap,
    }
