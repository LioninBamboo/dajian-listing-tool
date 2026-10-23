"""Registered-task table and allowlist entry-point tests for PR-A.

M1 golden lists were captured from 9b62e1f before any allowlist code landed.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest
import schedule

import scheduler_daemon
import scheduler_watchdog
from src.db.database_safety import DatabaseSafetyReport
from src.utils.store_profile import (
    KNOWN_SCHEDULER_JOBS,
    reset_store_profile_cache,
)

ROOT = Path(__file__).resolve().parents[1]
MAIN_PROFILE = ROOT / "config" / "store_profile.yaml"

_CATEGORY_TAGS = {"daily", "weekly", "recurring"}

# Captured from 9b62e1f via setup_schedule() on the default Main profile.
M1_GOLDEN_JOBS = [
    ("ad_restore", "days", "", "09:40", 1),
    ("analyze", "hours", "", "", 2),
    ("auto_publish", "days", "", "10:10", 1),
    ("bid_rollback", "weeks", "tuesday", "11:00", 1),
    ("bl_cleanup", "days", "", "09:45", 1),
    ("cro_consume", "days", "", "10:00", 1),
    ("cro_delist_email", "weeks", "monday", "11:00", 1),
    ("cro_fill_specifics", "days", "", "10:20", 1),
    ("cro_image_refresh", "days", "", "10:15", 1),
    ("cro_learn_thresholds", "weeks", "sunday", "02:00", 1),
    ("cro_lifecycle_detect", "days", "", "10:40", 1),
    ("cro_lifecycle_evaluate", "days", "", "10:50", 1),
    ("cro_monthly_report", "days", "", "09:55", 1),
    ("cro_ops_snapshot", "weeks", "sunday", "03:00", 1),
    ("cro_promote", "days", "", "10:25", 1),
    ("cro_promote_thresholds", "weeks", "sunday", "02:30", 1),
    ("cro_send_offer", "days", "", "10:35", 1),
    ("cro_sentinel", "days", "", "10:30", 1),
    ("cro_title_rewrite", "days", "", "10:45", 1),
    ("finance_sync", "days", "", "09:15", 1),
    ("finance_sync", "hours", "", "", 4),
    ("full", "days", "", "09:30", 1),
    ("giga_push", "days", "", "09:18", 1),
    ("giga_push", "hours", "", "", 4),
    ("giga_sync", "days", "", "09:22", 1),
    ("giga_sync", "hours", "", "", 4),
    ("guard_alert", "days", "", "09:50", 1),
    ("health", "days", "", "20:00", 1),
    ("listing_audit", "days", "", "11:30", 1),
    ("listing_sync", "days", "", "09:58", 1),
    ("mi_check", "days", "", "10:05", 1),
    ("mi_snapshot", "days", "", "10:00", 1),
    ("missing_video_autofix", "days", "", "13:00", 1),
    ("order_recheck", "hours", "", "", 6),
    ("promotion", "hours", "", "", 6),
    ("semantic_rewrite", "days", "", "12:30", 1),
    ("smart_bid", "weeks", "tuesday", "10:00", 1),
    ("source_aspect_autofix", "days", "", "12:10", 1),
    ("source_refresh", "days", "", "10:55", 1),
]

M1_GOLDEN_CALLABLES = [
    ("ad_restore", "days", "", "09:40", 1, "task_ad_restore"),
    ("analyze", "hours", "", "", 2, "task_auto_analyze"),
    ("auto_publish", "days", "", "10:10", 1, "task_auto_publish"),
    ("bid_rollback", "weeks", "tuesday", "11:00", 1, "task_bid_rollback"),
    ("bl_cleanup", "days", "", "09:45", 1, "task_blacklist_cleanup"),
    ("cro_consume", "days", "", "10:00", 1, "task_cro_consume"),
    ("cro_delist_email", "weeks", "monday", "11:00", 1, "task_cro_delist_email"),
    ("cro_fill_specifics", "days", "", "10:20", 1, "task_cro_fill_specifics"),
    ("cro_image_refresh", "days", "", "10:15", 1, "task_cro_image_refresh"),
    ("cro_learn_thresholds", "weeks", "sunday", "02:00", 1, "task_cro_learn_thresholds"),
    ("cro_lifecycle_detect", "days", "", "10:40", 1, "task_cro_lifecycle_detect"),
    ("cro_lifecycle_evaluate", "days", "", "10:50", 1, "task_cro_lifecycle_evaluate"),
    ("cro_monthly_report", "days", "", "09:55", 1, "task_cro_monthly_report"),
    ("cro_ops_snapshot", "weeks", "sunday", "03:00", 1, "task_cro_ops_snapshot"),
    ("cro_promote", "days", "", "10:25", 1, "task_cro_promote"),
    ("cro_promote_thresholds", "weeks", "sunday", "02:30", 1, "task_cro_promote_thresholds"),
    ("cro_send_offer", "days", "", "10:35", 1, "task_cro_send_offer"),
    ("cro_sentinel", "days", "", "10:30", 1, "task_cro_sentinel"),
    ("cro_title_rewrite", "days", "", "10:45", 1, "task_cro_title_rewrite"),
    ("finance_sync", "days", "", "09:15", 1, "task_finance_sync"),
    ("finance_sync", "hours", "", "", 4, "task_finance_sync"),
    ("full", "days", "", "09:30", 1, "task_daily_full"),
    ("giga_push", "days", "", "09:18", 1, "task_giga_dropship_push"),
    ("giga_push", "hours", "", "", 4, "task_giga_dropship_push"),
    ("giga_sync", "days", "", "09:22", 1, "task_giga_dropship_sync"),
    ("giga_sync", "hours", "", "", 4, "task_giga_dropship_sync"),
    ("guard_alert", "days", "", "09:50", 1, "task_guard_anomaly"),
    ("health", "days", "", "20:00", 1, "task_health_check"),
    ("listing_audit", "days", "", "11:30", 1, "task_listing_audit"),
    ("listing_sync", "days", "", "09:58", 1, "task_listing_status_sync"),
    ("mi_check", "days", "", "10:05", 1, "task_mi_self_check"),
    ("mi_snapshot", "days", "", "10:00", 1, "task_mi_snapshot"),
    ("missing_video_autofix", "days", "", "13:00", 1, "task_missing_video_autofix"),
    ("order_recheck", "hours", "", "", 6, "task_order_recheck"),
    ("promotion", "hours", "", "", 6, "task_promotion_rotate"),
    ("semantic_rewrite", "days", "", "12:30", 1, "task_semantic_rewrite"),
    ("smart_bid", "weeks", "tuesday", "10:00", 1, "task_smart_bid"),
    ("source_aspect_autofix", "days", "", "12:10", 1, "task_source_aspect_autofix"),
    ("source_refresh", "days", "", "10:55", 1, "task_source_refresh"),
]

M1_RECOVERY_NAMES = [
    "finance_sync",
    "giga_dropship_push",
    "giga_dropship_sync",
    "daily_tasks",
    "listing_status_sync",
    "mi_snapshot",
    "auto_publish",
    "mi_self_check",
    "ad_restore",
    "blacklist_cleanup",
    "guard_anomaly",
    "cro_monthly_report",
    "cro_consume",
    "smart_bid",
    "cro_image_refresh",
    "cro_fill_specifics",
    "cro_promote",
    "cro_sentinel",
    "cro_send_offer",
    "cro_lifecycle_detect",
    "cro_title_rewrite",
    "cro_lifecycle_evaluate",
    "bid_rollback",
    "cro_delist_email",
    "source_refresh",
    "listing_audit",
    "source_aspect_autofix",
    "semantic_rewrite",
    "missing_video_autofix",
    "health_check",
    "cro_learn_thresholds",
    "cro_promote_thresholds",
    "cro_ops_snapshot",
]

M1_WATCHDOG_NAMES = [
    "daily_tasks",
    "mi_self_check",
    "ad_restore",
    "blacklist_cleanup",
    "guard_anomaly",
    "cro_monthly_report",
    "cro_consume",
    "smart_bid",
    "cro_image_refresh",
    "cro_fill_specifics",
    "cro_promote",
    "cro_sentinel",
    "bid_rollback",
    "cro_delist_email",
    "listing_audit",
    "health_check",
]

OPS_DEFAULT_JOBS = [
    ("ops", "days", "", "09:30", 1),
    ("order_recheck", "hours", "", "", 6),
]

FORBIDDEN_AQUA_TAGS = {
    "trading_content_qc",
    "fitment_audit",
    "fitment_apply",
    "smart_reprice",
    "ad_restore",
    "ads_enroll",
    "bl_cleanup",
    "guard_alert",
    "promotion",
    "listing_audit",
    "source_aspect_autofix",
    "semantic_rewrite",
    "missing_video_autofix",
    "source_refresh",
}

GP_G1_YAML = """
server:
  scheduler_profile: ops
  scheduler_enable_ads: true
  scheduler_enable_qc_autofix: true
  scheduler_mutex_name: Global\\GrovePopSchedulerDaemonMutex
store:
  brand_name: GrovePop
"""

AQUA_A1_YAML = """
server:
  scheduler_profile: ops
  scheduler_enable_ads: true
  scheduler_enable_qc_autofix: false
  scheduler_mutex_name: Global\\AquaRidesSchedulerDaemonMutex
ebay:
  listing_channel: trading
  ebay_site_id: "100"
quality_gate:
  qc_profile: motors
store:
  brand_name: AquaRides
"""


def _job_tag(job) -> str:
    specific = [tag for tag in job.tags if tag not in _CATEGORY_TAGS]
    assert specific, f"job has no identifying tag: {job.tags}"
    return specific[0]


def _at_time(job) -> str:
    value = getattr(job, "at_time", None)
    if value is None:
        return ""
    return value.strftime("%H:%M")


def _registered_rows():
    rows = []
    for job in schedule.get_jobs():
        rows.append(
            (
                _job_tag(job),
                job.unit or "",
                job.start_day or "",
                _at_time(job),
                job.interval,
            )
        )
    return sorted(rows)


def _registered_callables():
    rows = []
    for job in schedule.get_jobs():
        fn = job.job_func
        name = getattr(getattr(fn, "func", fn), "__name__", None)
        rows.append(
            (
                _job_tag(job),
                job.unit or "",
                job.start_day or "",
                _at_time(job),
                job.interval,
                name,
            )
        )
    return sorted(rows)


def _write_profile(tmp_path: Path, body: str, name: str = "profile.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return path


def _activate_profile(monkeypatch, path: Path) -> None:
    monkeypatch.setenv("STORE_PROFILE_PATH", str(path))
    reset_store_profile_cache()


def _recovery_names():
    return [task["name"] for task in scheduler_daemon._get_critical_recovery_tasks()]


def _watchdog_names():
    return [task["health_name"] for task in scheduler_watchdog._watchdog_critical_tasks()]


@pytest.fixture(autouse=True)
def _isolate_schedule_and_profile():
    schedule.clear()
    reset_store_profile_cache()
    yield
    schedule.clear()
    reset_store_profile_cache()


def test_known_scheduler_jobs_exclude_trading_fitment():
    assert "trading_content_qc" not in KNOWN_SCHEDULER_JOBS
    assert "fitment_audit" not in KNOWN_SCHEDULER_JOBS
    assert "fitment_apply" not in KNOWN_SCHEDULER_JOBS


@pytest.mark.parametrize(
    ("case_id", "yaml_body", "profile_path", "expected_jobs", "recovery", "watchdog"),
    [
        (
            "M1",
            None,
            MAIN_PROFILE,
            M1_GOLDEN_JOBS,
            M1_RECOVERY_NAMES,
            M1_WATCHDOG_NAMES,
        ),
        (
            "M2",
            """
            server:
              scheduler_profile: daily
            """,
            None,
            M1_GOLDEN_JOBS,
            M1_RECOVERY_NAMES,
            M1_WATCHDOG_NAMES,
        ),
        (
            "M3",
            """
            server:
              scheduler_profile: full
              scheduler_allowed_jobs:
                - ops_daily
            """,
            None,
            M1_GOLDEN_JOBS,
            M1_RECOVERY_NAMES,
            M1_WATCHDOG_NAMES,
        ),
        (
            "G1",
            GP_G1_YAML,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "A1",
            AQUA_A1_YAML,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "G4",
            """
            server:
              scheduler_profile: ops
              scheduler_enable_ads: true
              scheduler_enable_qc_autofix: true
              scheduler_mutex_name: Global\\GrovePopSchedulerDaemonMutex
              scheduler_allowed_jobs:
                - ops_daily
                - order_recheck
                - listing_audit
            store:
              brand_name: GrovePop
            """,
            None,
            [
                ("listing_audit", "days", "", "11:30", 1),
                ("ops", "days", "", "09:30", 1),
                ("order_recheck", "hours", "", "", 6),
            ],
            ["ops_daily", "listing_audit"],
            ["ops_daily", "listing_audit"],
        ),
        (
            "S1",
            """
            server:
              scheduler_profile: ops
              scheduler_allowed_jobs:
                - ops_daily
            """,
            None,
            [("ops", "days", "", "09:30", 1)],
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "S2",
            """
            server:
              scheduler_profile: ops
              scheduler_enable_ads: false
              scheduler_allowed_jobs:
                - ops_daily
                - order_recheck
                - smart_reprice
            """,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "S3",
            """
            server:
              scheduler_profile: ops
              scheduler_enable_ads: true
              scheduler_allowed_jobs:
                - ops_daily
                - order_recheck
                - smart_reprice
            """,
            None,
            [
                ("ops", "days", "", "09:30", 1),
                ("order_recheck", "hours", "", "", 6),
                ("smart_reprice", "weeks", "monday", "09:35", 1),
                ("smart_reprice", "weeks", "thursday", "09:35", 1),
            ],
            ["ops_daily", "smart_reprice", "smart_reprice"],
            ["ops_daily", "smart_reprice", "smart_reprice"],
        ),
        (
            "S4",
            """
            server:
              scheduler_profile: ops
              scheduler_allowed_jobs:
                - ops_daily
                - bogus_job
            """,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "S6",
            """
            server:
              scheduler_profile: "OPS "
            """,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "S7",
            """
            server:
              scheduler_profile: dialy
            """,
            None,
            M1_GOLDEN_JOBS,
            M1_RECOVERY_NAMES,
            M1_WATCHDOG_NAMES,
        ),
        (
            "S8",
            """
            server:
              scheduler_profile: ops
              scheduler_enable_ads: true
              scheduler_enable_qc_autofix: true
              scheduler_allowed_jobs: []
            """,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "S9",
            """
            server:
              scheduler_profile: ops
              scheduler_allowed_jobs:
                - trading_content_qc
                - fitment_apply
            """,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "S10",
            """
            server:
              scheduler_profile: ops
              scheduler_enable_qc_autofix: true
              scheduler_allowed_jobs: "ops_daily, order_recheck, listing_audit"
            """,
            None,
            [
                ("listing_audit", "days", "", "11:30", 1),
                ("ops", "days", "", "09:30", 1),
                ("order_recheck", "hours", "", "", 6),
            ],
            ["ops_daily", "listing_audit"],
            ["ops_daily", "listing_audit"],
        ),
        (
            "S11",
            """
            server:
              scheduler_profile: ops
              scheduler_allowed_jobs:
                - ops_daily
                - ops_daily
                - order_recheck
            """,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "S12",
            """
            server:
              scheduler_profile: ops
              scheduler_allowed_jobs:
                - "  ops_daily  "
                - order_recheck
            """,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
        (
            "S13",
            """
            server:
              scheduler_profile: ops
              scheduler_enable_qc_autofix: false
              scheduler_allowed_jobs:
                - ops_daily
                - order_recheck
                - listing_audit
            """,
            None,
            OPS_DEFAULT_JOBS,
            ["ops_daily"],
            ["ops_daily"],
        ),
    ],
    ids=[
        "M1",
        "M2",
        "M3",
        "G1",
        "A1",
        "G4",
        "SYNTHETIC-S1",
        "SYNTHETIC-S2",
        "SYNTHETIC-S3",
        "SYNTHETIC-S4",
        "SYNTHETIC-S6",
        "SYNTHETIC-S7",
        "SYNTHETIC-S8",
        "SYNTHETIC-S9",
        "SYNTHETIC-S10",
        "SYNTHETIC-S11",
        "SYNTHETIC-S12",
        "SYNTHETIC-S13",
    ],
)
def test_registered_task_table(
    tmp_path,
    monkeypatch,
    case_id,
    yaml_body,
    profile_path,
    expected_jobs,
    recovery,
    watchdog,
):
    path = Path(profile_path) if profile_path is not None else _write_profile(tmp_path, yaml_body)
    _activate_profile(monkeypatch, path)
    scheduler_daemon.setup_schedule()
    assert _registered_rows() == expected_jobs
    assert _recovery_names() == recovery
    assert _watchdog_names() == watchdog
    if case_id == "M1":
        assert _registered_callables() == M1_GOLDEN_CALLABLES
    if case_id == "A1":
        tags = {tag for tag, *_ in _registered_rows()}
        assert tags.isdisjoint(FORBIDDEN_AQUA_TAGS)
        assert "trading_content_qc" not in tags
        assert "fitment_apply" not in tags


def test_SYNTHETIC_S2_logs_allowlist_but_ads_slice_off(tmp_path, monkeypatch, caplog):
    path = _write_profile(
        tmp_path,
        """
        server:
          scheduler_profile: ops
          scheduler_enable_ads: false
          scheduler_allowed_jobs:
            - ops_daily
            - order_recheck
            - smart_reprice
        """,
    )
    _activate_profile(monkeypatch, path)
    with caplog.at_level(logging.INFO):
        scheduler_daemon.setup_schedule()
    assert any("on allowlist but ads slice off" in record.message for record in caplog.records)
    assert _registered_rows() == OPS_DEFAULT_JOBS


def test_SYNTHETIC_S5_unreadable_ops_yaml_fail_closed(tmp_path, monkeypatch, caplog):
    path = tmp_path / "broken.yaml"
    path.write_text(
        "server:\n  scheduler_profile: ops\nstore: [unclosed\n",
        encoding="utf-8",
    )
    _activate_profile(monkeypatch, path)
    with caplog.at_level(logging.WARNING):
        scheduler_daemon.setup_schedule()
    assert _registered_rows() == OPS_DEFAULT_JOBS
    assert any("unreadable" in record.message for record in caplog.records)
    assert any("fail-closed" in record.message for record in caplog.records)
    tags = {tag for tag, *_ in _registered_rows()}
    assert "full" not in tags
    assert "mi_snapshot" not in tags


def test_G2_run_task_skips_smart_reprice(tmp_path, monkeypatch):
    _activate_profile(monkeypatch, _write_profile(tmp_path, GP_G1_YAML))
    spawned = []

    def fake_popen(*args, **kwargs):
        spawned.append((args, kwargs))
        raise AssertionError("must not spawn a subprocess for a disallowed ops job")

    monkeypatch.setattr(scheduler_daemon.subprocess, "Popen", fake_popen)
    ok, msg = scheduler_daemon.run_task(
        "smart_reprice",
        ["python", "-c", "print(1)"],
    )
    assert ok is False
    assert msg == "skipped: not on ops allowlist (smart_reprice)"
    assert spawned == []


def _invoke_cli_task(monkeypatch, tmp_path, task_name: str):
    class DummyReport(DatabaseSafetyReport):
        pass

    monkeypatch.setattr(
        scheduler_daemon,
        "validate_runtime_database",
        lambda *args, **kwargs: DummyReport(
            path=tmp_path / "unused.db",
            link_count=1,
            integrity_check="ok",
            page_count=1,
            size_bytes=1,
        ),
    )
    monkeypatch.setattr(scheduler_daemon, "MAINTENANCE_FILE", tmp_path / "no.lock")
    monkeypatch.setattr(
        scheduler_daemon.sys,
        "argv",
        ["scheduler_daemon.py", "--task", task_name],
    )
    scheduler_daemon.main()


def test_G3_task_cli_and_recovery_and_watchdog(tmp_path, monkeypatch):
    _activate_profile(monkeypatch, _write_profile(tmp_path, GP_G1_YAML))
    called = []

    def track(name):
        def _inner(*args, **kwargs):
            called.append(name)
            return True, "ok"

        return _inner

    for attr in (
        "task_smart_reprice",
        "task_promotion_rotate",
        "task_listing_audit",
        "task_ops_daily",
        "task_ad_restore",
        "task_ads_enroll",
        "task_source_aspect_autofix",
        "task_semantic_rewrite",
        "task_missing_video_autofix",
        "task_source_refresh",
    ):
        monkeypatch.setattr(scheduler_daemon, attr, track(attr))

    for alias in ("reprice", "promotion", "listing_audit"):
        before = list(called)
        _invoke_cli_task(monkeypatch, tmp_path, alias)
        assert called == before

    _invoke_cli_task(monkeypatch, tmp_path, "daily")
    assert called == ["task_ops_daily"]

    health = tmp_path / "_scheduler_health.json"
    health.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scheduler_daemon, "HEALTH_FILE", health)
    recovered = []

    def recover_ops():
        recovered.append("ops_daily")
        return True, "ok"

    def recover_ads_or_qc(name):
        def _inner(*args, **kwargs):
            recovered.append(name)
            return True, "ok"

        return _inner

    monkeypatch.setattr(scheduler_daemon, "task_ops_daily", recover_ops)
    for attr, name in (
        ("task_smart_reprice", "smart_reprice"),
        ("task_ad_restore", "ad_restore"),
        ("task_ads_enroll", "ads_enroll"),
        ("task_listing_audit", "listing_audit"),
        ("task_source_aspect_autofix", "source_aspect_autofix"),
    ):
        monkeypatch.setattr(scheduler_daemon, attr, recover_ads_or_qc(name))

    scheduler_daemon.recover_missed_tasks()
    assert "smart_reprice" not in recovered
    assert "ad_restore" not in recovered
    assert "ads_enroll" not in recovered
    assert "listing_audit" not in recovered
    assert "source_aspect_autofix" not in recovered

    assert _watchdog_names() == ["ops_daily"]
