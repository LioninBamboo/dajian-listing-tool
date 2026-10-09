import importlib.util
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


def _job_tags(mod=None):
    return {frozenset(job.tags) for job in schedule.get_jobs()}


def test_ops_qc_autofix_slice_registers_main_store_qc_chain(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    profile = StoreProfile(
        scheduler_profile="ops",
        brand_name="GrovePop",
        scheduler_enable_qc_autofix=True,
    )
    monkeypatch.setattr(sp, "get_store_profile", lambda: profile)

    mod = _load_scheduler()
    mod.setup_schedule()

    tags = _job_tags()
    assert frozenset({"daily", "ops"}) in tags
    assert frozenset({"recurring", "order_recheck"}) in tags
    assert frozenset({"daily", "listing_audit"}) in tags
    assert frozenset({"daily", "source_aspect_autofix"}) in tags
    assert frozenset({"daily", "missing_video_autofix"}) in tags
    assert frozenset({"daily", "semantic_rewrite"}) in tags
    assert frozenset({"daily", "source_refresh"}) in tags
    assert frozenset({"daily", "mi_snapshot"}) not in tags
    assert frozenset({"daily", "cro_consume"}) not in tags
    assert frozenset({"daily", "auto_publish"}) not in tags


def test_ops_ads_slice_registers_ad_chain_without_cro_consume(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    profile = StoreProfile(
        scheduler_profile="ops",
        brand_name="AquaRides",
        scheduler_enable_ads=True,
    )
    monkeypatch.setattr(sp, "get_store_profile", lambda: profile)

    mod = _load_scheduler()
    mod.setup_schedule()

    tags = _job_tags()
    assert frozenset({"daily", "ops"}) in tags
    assert frozenset({"daily", "ad_restore"}) in tags
    assert frozenset({"daily", "bl_cleanup"}) in tags
    assert frozenset({"daily", "guard_alert"}) in tags
    assert frozenset({"daily", "ads_enroll"}) in tags
    assert frozenset({"weekly", "smart_bid"}) in tags
    assert frozenset({"weekly", "bid_rollback"}) in tags
    assert frozenset({"weekly", "smart_reprice"}) in tags
    assert frozenset({"recurring", "promotion"}) in tags
    assert frozenset({"daily", "cro_consume"}) not in tags
    assert frozenset({"daily", "cro_promote"}) not in tags
    assert frozenset({"daily", "mi_snapshot"}) not in tags
    assert frozenset({"daily", "listing_audit"}) not in tags


def test_ops_qc_recovery_includes_audit_chain(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    monkeypatch.setattr(
        sp,
        "get_store_profile",
        lambda: StoreProfile(
            scheduler_profile="ops",
            brand_name="GrovePop",
            scheduler_enable_qc_autofix=True,
        ),
    )

    mod = _load_scheduler()
    names = [t["name"] for t in mod._get_critical_recovery_tasks()]
    assert names[0] == "ops_daily"
    assert "listing_audit" in names
    assert "source_aspect_autofix" in names
    assert "daily_tasks" not in names
    assert "cro_consume" not in names


def test_ops_ads_recovery_includes_enroll_and_restore(monkeypatch):
    from src.utils import store_profile as sp
    from src.utils.store_profile import StoreProfile

    monkeypatch.setattr(
        sp,
        "get_store_profile",
        lambda: StoreProfile(
            scheduler_profile="ops",
            brand_name="AquaRides",
            scheduler_enable_ads=True,
        ),
    )

    mod = _load_scheduler()
    names = [t["name"] for t in mod._get_critical_recovery_tasks()]
    assert names[0] == "ops_daily"
    assert "ads_enroll" in names
    assert "ad_restore" in names
    assert "smart_reprice" in names
    assert "daily_tasks" not in names
    assert "cro_promote" not in names
