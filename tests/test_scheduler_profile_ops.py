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
