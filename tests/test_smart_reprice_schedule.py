"""Shared blanket-reprice cadence for all store instances."""
from datetime import datetime

from src.utils.smart_reprice_schedule import (
    SMART_REPRICE_RUN_DAY_LABELS,
    should_run_smart_reprice,
    smart_reprice_skip_reason,
)


def test_blanket_reprice_runs_monday_and_thursday_only():
    monday = datetime(2026, 8, 24, 10, 45)
    thursday = datetime(2026, 8, 20, 10, 45)
    tuesday = datetime(2026, 8, 25, 10, 45)
    assert should_run_smart_reprice(monday) is True
    assert should_run_smart_reprice(thursday) is True
    assert should_run_smart_reprice(tuesday) is False
    assert smart_reprice_skip_reason(tuesday)
    assert "周一" in SMART_REPRICE_RUN_DAY_LABELS
    assert smart_reprice_skip_reason(monday) == ""
