"""GrovePop marketing digest uses CRO title rewrite, not paused daily_optimize."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "grove_marketing_ops_digest", ROOT / "scripts" / "marketing_ops_digest.py"
)
digest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = digest
SPEC.loader.exec_module(digest)


def test_title_rewrite_argv_applies_when_flag_on(monkeypatch):
    monkeypatch.setenv("ENABLE_SCHEDULED_TITLE_REWRITE_APPLY", "1")
    argv, skip = digest.build_title_rewrite_argv()
    assert skip == ""
    assert argv is not None
    joined = " ".join(argv)
    assert "cro_title_rewrite.py" in joined
    assert "daily_optimize.py" not in joined
    assert "--apply" in argv
    assert "--yes" in argv
    assert "--email" in argv


def test_title_rewrite_argv_is_dry_run_when_flag_off(monkeypatch):
    monkeypatch.setenv("ENABLE_SCHEDULED_TITLE_REWRITE_APPLY", "0")
    argv, skip = digest.build_title_rewrite_argv()
    assert skip == ""
    assert argv is not None
    assert "cro_title_rewrite.py" in " ".join(argv)
    assert "--apply" not in argv


def test_paused_jobs_do_not_include_daily_optimize():
    labels = [label for label, _argv, _enabled in digest.JOBS]
    assert "标题优化" not in labels
    for _label, argv, _enabled in digest.JOBS:
        assert "daily_optimize.py" not in " ".join(argv)


def test_digest_includes_finance_sync_job():
    labels = [label for label, argv, _enabled in digest.JOBS]
    assert "财务订单同步" in labels
    finance = next(argv for label, argv, _enabled in digest.JOBS if label == "财务订单同步")
    assert "finance_sync_orders.py" in " ".join(finance)
