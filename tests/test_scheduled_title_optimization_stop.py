import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

DAILY_OPTIMIZE_SPEC = importlib.util.spec_from_file_location(
    "daily_optimize",
    ROOT / "src" / "plugins" / "active_listing_optimizer" / "daily_optimize.py",
)
daily_optimize = importlib.util.module_from_spec(DAILY_OPTIMIZE_SPEC)
DAILY_OPTIMIZE_SPEC.loader.exec_module(daily_optimize)


def test_scheduled_title_optimization_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_SCHEDULED_TITLE_OPTIMIZATION", raising=False)

    assert daily_optimize.scheduled_title_optimization_disabled() is True


def test_scheduled_title_optimization_requires_explicit_override(monkeypatch):
    monkeypatch.setenv("ENABLE_SCHEDULED_TITLE_OPTIMIZATION", "1")

    assert daily_optimize.scheduled_title_optimization_disabled() is False
