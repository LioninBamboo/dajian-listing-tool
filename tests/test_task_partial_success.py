from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

WATCHDOG_SPEC = importlib.util.spec_from_file_location(
    "scheduler_watchdog_partial_success", ROOT / "scheduler_watchdog.py"
)
scheduler_watchdog = importlib.util.module_from_spec(WATCHDOG_SPEC)
WATCHDOG_SPEC.loader.exec_module(scheduler_watchdog)


class FixedMondayNoonDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 5, 11, 12, 0, 0)


def test_daily_outcome_is_partial_when_only_reprice_items_fail():
    from src.utils.task_result_status import classify_daily_task_outcome

    results = {
        "inventory": {"checked": 10, "errors": 0},
        "smart_reprice": {
            "status": "ok",
            "summary": {"price_changes": 9, "errors": 3},
            "results": [
                {"sku": "OK-1", "status": "updated"},
                {"sku": "FAIL-1", "status": "error"},
            ],
        },
    }

    assert classify_daily_task_outcome(results) == "partial_success"


def test_watchdog_does_not_recover_daily_task_after_partial_success(tmp_path, monkeypatch):
    health_path = tmp_path / "_scheduler_health.json"
    health_path.write_text(
        json.dumps(
            {
                "tasks": {
                    "daily_tasks": {
                        "status": "partial_success",
                        "at": "2026-05-11T11:30:00",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    launched = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(scheduler_watchdog, "HEALTH_FILE", health_path)
    monkeypatch.setattr(scheduler_watchdog, "datetime", FixedMondayNoonDateTime)
    monkeypatch.setattr(scheduler_watchdog, "log", lambda _message: None)
    monkeypatch.setattr(scheduler_watchdog.subprocess, "Popen", FakePopen)

    scheduler_watchdog.check_overdue_critical_tasks()

    assert "daily" not in [command[-1] for command in launched]
