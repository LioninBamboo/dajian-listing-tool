"""Shared cadence for the blanket (category-average) smart reprice.

All store instances use this. Conversion-driven CRO queue consume is a
separate daily path and is not gated here.
"""
from __future__ import annotations

from datetime import datetime

SMART_REPRICE_RUN_DAYS = {0, 3}  # Monday / Thursday
SMART_REPRICE_RUN_DAY_LABELS = "周一、周四"


def should_run_smart_reprice(run_time: datetime | None = None) -> bool:
    """Full-catalog smart reprice runs twice a week on every store."""
    run_time = run_time or datetime.now()
    return run_time.weekday() in SMART_REPRICE_RUN_DAYS


def smart_reprice_skip_reason(run_time: datetime | None = None) -> str:
    if should_run_smart_reprice(run_time):
        return ""
    return (
        f"全库智能调价仅 {SMART_REPRICE_RUN_DAY_LABELS} 执行；"
        "近 14 天出单 SKU 由成交冷静期保护，不在全库重定价里改价"
    )
