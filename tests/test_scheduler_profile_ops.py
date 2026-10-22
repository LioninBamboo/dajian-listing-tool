import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
import schedule

ROOT = Path(__file__).resolve().parents[1]


def _load_scheduler():
    spec = importlib.util.spec_from_file_location("scheduler_daemon", ROOT / "scheduler_daemon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def clear_schedule():
    schedule.clear()
    yield
    schedule.clear()


def test_ops_profile_registers_only_inventory_and_order_recheck(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    profile = StoreProfile(scheduler_profile="ops", brand_name="GrovePop")
    monkeypatch.setattr(sp, "get_store_profile", lambda: profile)

    mod = _load_scheduler()
    mod.setup_schedule()

    jobs = schedule.get_jobs()
    assert len(jobs) == 2
    tags = {frozenset(job.tags) for job in jobs}
    assert frozenset({"daily", "ops"}) in tags
    assert frozenset({"recurring", "order_recheck"}) in tags


def test_full_profile_registers_cro_and_mi_tasks(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    monkeypatch.setattr(sp, "get_store_profile", lambda: StoreProfile())

    mod = _load_scheduler()
    mod.setup_schedule()

    jobs = schedule.get_jobs()
    assert len(jobs) > 10
    tags = {frozenset(job.tags) for job in jobs}
    assert frozenset({"daily", "mi_snapshot"}) in tags
    assert frozenset({"daily", "cro_consume"}) in tags


def test_ops_recovery_tasks_list_is_minimal(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    monkeypatch.setattr(
        sp,
        "get_store_profile",
        lambda: StoreProfile(scheduler_profile="ops", brand_name="GrovePop"),
    )

    mod = _load_scheduler()
    tasks = mod._get_critical_recovery_tasks()
    assert [t["name"] for t in tasks] == ["ops_daily"]


def test_ops_task_daily_full_dispatches_to_ops_only(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    monkeypatch.setattr(
        sp,
        "get_store_profile",
        lambda: StoreProfile(scheduler_profile="ops", brand_name="GrovePop"),
    )

    mod = _load_scheduler()
    monkeypatch.setattr(mod, "_task_succeeded_today", lambda *args, **kwargs: False)
    calls = []

    def fake_run_task(task_name, cmd_args, timeout_sec=3600):
        calls.append((task_name, list(cmd_args), timeout_sec))
        return True, "ok"

    monkeypatch.setattr(mod, "run_task", fake_run_task)
    mod.task_daily_full()

    assert len(calls) == 1
    task_name, cmd_args, timeout_sec = calls[0]
    assert task_name == "ops_daily"
    assert any(str(arg).endswith("daily_tasks.py") for arg in cmd_args)
    assert "--ops-only" in cmd_args
    assert timeout_sec == mod.TASK_TIMEOUT["ops_daily"]


def _gp_ops_profile(**kwargs):
    from src.utils.store_profile import StoreProfile

    values = {
        "scheduler_profile": "ops",
        "brand_name": "GrovePop",
        "scheduler_enable_ads": True,
        "scheduler_enable_qc_autofix": True,
    }
    values.update(kwargs)
    return StoreProfile(**values)


def _job_tags(jobs):
    return {frozenset(job.tags) for job in jobs}


def test_gp_profile_schedules_exactly_ops_daily_and_order_recheck(monkeypatch):
    from src.utils import store_profile as sp

    monkeypatch.setattr(sp, "get_store_profile", lambda: _gp_ops_profile())
    mod = _load_scheduler()
    mod.setup_schedule()
    jobs = schedule.get_jobs()
    assert len(jobs) == 2
    assert _job_tags(jobs) == {
        frozenset({"daily", "ops"}),
        frozenset({"recurring", "order_recheck"}),
    }


def test_gp_does_not_schedule_thursday_0935_reprice(monkeypatch):
    from src.utils import store_profile as sp

    monkeypatch.setattr(sp, "get_store_profile", lambda: _gp_ops_profile())
    mod = _load_scheduler()
    mod.setup_schedule()
    for job in schedule.get_jobs():
        assert "smart_reprice" not in job.tags
        assert getattr(job, "start_day", None) != "thursday"


def test_main_daily_profile_schedule_unchanged(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    monkeypatch.setattr(sp, "get_store_profile", lambda: StoreProfile())
    mod = _load_scheduler()
    mod.setup_schedule()
    tags = _job_tags(schedule.get_jobs())
    assert frozenset({"daily", "full"}) in tags
    assert frozenset({"daily", "mi_snapshot"}) in tags
    assert frozenset({"daily", "cro_consume"}) in tags
    assert frozenset({"daily", "listing_audit"}) in tags
    # Main reprice stays inside daily_tasks (Mon/Thu), not a standalone 09:35 job.
    assert not any("smart_reprice" in job.tags for job in schedule.get_jobs())
    assert frozenset({"daily", "ad_restore"}) in tags
    assert frozenset({"recurring", "promotion"}) in tags
    assert frozenset({"daily", "finance_sync"}) in tags
    assert frozenset({"daily", "health"}) in tags


class _Oct22Afternoon(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 22, 15, 5, 0)


def test_ops_catchup_after_12_day_outage_only_ops_daily(tmp_path, monkeypatch):
    """10/10 → 10/22 outage: catch-up must not backfill reprice/CRO/MI/audit."""
    from src.utils import store_profile as sp

    health = tmp_path / "_scheduler_health.json"
    health.write_text(
        '{"tasks": {"ops_daily": {"status": "success", "at": "2026-10-10T09:31:00"},'
        ' "daily_tasks": {"status": "success", "at": "2026-10-10T09:31:00"},'
        ' "smart_reprice": {"status": "success", "at": "2026-10-09T09:35:00"},'
        ' "listing_audit": {"status": "success", "at": "2026-10-10T11:30:00"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(sp, "get_store_profile", lambda: _gp_ops_profile())
    mod = _load_scheduler()
    monkeypatch.setattr(mod, "HEALTH_FILE", health)
    monkeypatch.setattr(mod, "datetime", _Oct22Afternoon)
    recovered = []
    monkeypatch.setattr(mod, "task_ops_daily", lambda: recovered.append("ops_daily"))
    monkeypatch.setattr(mod, "task_smart_reprice", lambda: recovered.append("smart_reprice"))
    monkeypatch.setattr(mod, "task_ad_restore", lambda: recovered.append("ad_restore"))
    monkeypatch.setattr(mod, "task_listing_audit", lambda: recovered.append("listing_audit"))
    monkeypatch.setattr(mod, "task_source_aspect_autofix", lambda: recovered.append("source_aspect_autofix"))
    monkeypatch.setattr(mod, "task_ads_enroll", lambda: recovered.append("ads_enroll"))

    # Recovery table still holds the original funcs; rebuild after patches.
    tasks = mod._get_critical_recovery_tasks()
    assert [t["name"] for t in tasks] == ["ops_daily"]
    for task in tasks:
        if task["name"] == "ops_daily":
            task["func"] = lambda: recovered.append("ops_daily")

    monkeypatch.setattr(mod, "_get_critical_recovery_tasks", lambda: tasks)
    mod.recover_missed_tasks(log_when_clean=False)
    assert recovered == ["ops_daily"]


def test_unreadable_ops_profile_fail_closed_to_two_jobs(monkeypatch):
    from src.utils import store_profile as sp

    def boom():
        raise RuntimeError("cannot read store profile")

    monkeypatch.setattr(sp, "get_store_profile", boom)
    monkeypatch.setattr(sp, "peek_yaml_says_ops", lambda *a, **k: True)
    mod = _load_scheduler()
    monkeypatch.setattr(mod, "get_store_profile", boom)
    # peek_yaml_says_ops is imported inside the fail-closed helpers.
    mod.setup_schedule()
    assert len(schedule.get_jobs()) == 2
    assert _job_tags(schedule.get_jobs()) == {
        frozenset({"daily", "ops"}),
        frozenset({"recurring", "order_recheck"}),
    }


def test_unknown_allowlist_entries_fail_closed(monkeypatch):
    from src.utils import store_profile as sp

    monkeypatch.setattr(
        sp,
        "get_store_profile",
        lambda: _gp_ops_profile(scheduler_allowed_jobs=("not-a-job", "smart_reprice")),
    )
    mod = _load_scheduler()
    mod.setup_schedule()
    assert len(schedule.get_jobs()) == 2
    for job in schedule.get_jobs():
        assert "smart_reprice" not in job.tags


def test_run_task_skips_disallowed_apply_jobs_on_gp(monkeypatch):
    from src.utils import store_profile as sp

    monkeypatch.setattr(sp, "get_store_profile", lambda: _gp_ops_profile())
    mod = _load_scheduler()
    spawned = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(AssertionError("must not spawn")))
    ok, msg = mod.run_task("listing_audit", ["python", "-c", "print(1)"])
    assert ok is False
    assert "allowlist" in msg
    assert spawned == []
    ok, msg = mod.run_task("smart_reprice", ["python", "-c", "print(1)"])
    assert ok is False
    assert spawned == []


def test_run_once_on_gp_skips_listing_audit_and_reprice(monkeypatch):
    from src.utils import store_profile as sp

    monkeypatch.setattr(sp, "get_store_profile", lambda: _gp_ops_profile())
    mod = _load_scheduler()
    called = []
    monkeypatch.setattr(mod, "task_listing_audit", lambda: called.append("listing_audit"))
    monkeypatch.setattr(mod, "task_smart_reprice", lambda: called.append("smart_reprice"))
    monkeypatch.setattr(mod, "task_ops_daily", lambda: called.append("ops_daily") or (True, "ok"))
    mod.run_once()
    assert called == ["ops_daily"]


def test_gp_setup_logs_skip_why_for_apply_jobs(monkeypatch, caplog):
    from src.utils import store_profile as sp

    monkeypatch.setattr(sp, "get_store_profile", lambda: _gp_ops_profile())
    mod = _load_scheduler()
    with caplog.at_level("INFO"):
        mod.setup_schedule()
    text = caplog.text
    for job in (
        "smart_reprice",
        "ad_restore",
        "listing_audit",
        "source_aspect_autofix",
        "semantic_rewrite",
        "missing_video_autofix",
        "promotion_rotate",
    ):
        assert f"skip {job}" in text
        assert "not on ops allowlist" in text
