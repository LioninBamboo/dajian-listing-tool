"""Marketing digest still runs smart reprice; the writer picks the channel."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "marketing_ops_digest", ROOT / "scripts" / "marketing_ops_digest.py"
)
digest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = digest
SPEC.loader.exec_module(digest)


def test_trading_store_runs_smart_reprice():
    jobs = digest.build_digest_jobs(SimpleNamespace(listing_channel="trading"))
    smart = next(job for job in jobs if "Smart Price" in job.label)
    assert smart.enabled is True
    assert smart.argv is not None
    assert smart.argv[-1] == "--apply"
    assert "batch_smart_reprice.py" in smart.argv[0]


def test_inventory_store_keeps_smart_reprice():
    jobs = digest.build_digest_jobs(SimpleNamespace(listing_channel="inventory"))
    smart = next(job for job in jobs if "Smart Price" in job.label)
    assert smart.enabled is True
    assert smart.argv is not None
    assert smart.argv[-1] == "--apply"


def test_digest_includes_finance_sync_job():
    jobs = digest.build_digest_jobs(SimpleNamespace(listing_channel="trading"))
    finance = next(job for job in jobs if "财务" in job.label)
    assert finance.enabled is True
    assert finance.argv is not None
    assert "finance_sync_orders.py" in " ".join(finance.argv)


def test_latest_mi_ready_skus_reads_newest_snapshot(tmp_path):
    from datetime import datetime

    day = datetime.now().strftime("%Y%m%d")
    older = tmp_path / f"mi_opportunities_{day}_100000.json"
    newer = tmp_path / f"mi_opportunities_{day}_111210.json"
    older.write_text('{"opportunities":[{"sku":"OLD","status":"READY"}]}', encoding="utf-8")
    newer.write_text(
        '{"opportunities":[{"sku":"NEW1","status":"READY"},{"sku":"SKIP","status":"PENDING"}]}',
        encoding="utf-8",
    )
    older.touch()
    import time
    time.sleep(0.05)
    newer.write_text(
        '{"opportunities":[{"sku":"NEW1","status":"READY"},{"sku":"SKIP","status":"PENDING"}]}',
        encoding="utf-8",
    )
    skus = digest.latest_mi_ready_skus(tmp_path, limit=10)
    assert skus == ["NEW1"]


def test_title_job_uses_cro_title_rewrite_and_applies_when_flag_on(monkeypatch):
    monkeypatch.setenv("ENABLE_SCHEDULED_TITLE_REWRITE_APPLY", "1")
    jobs = digest.build_digest_jobs(SimpleNamespace(listing_channel="trading"))
    title = next(job for job in jobs if "标题" in job.label)
    assert title.enabled is True
    assert title.argv is not None
    joined = " ".join(title.argv)
    assert "cro_title_rewrite.py" in joined
    assert "daily_optimize.py" not in joined
    assert "--apply" in title.argv
    assert "--yes" in title.argv
    assert "--email" in title.argv


def test_title_job_stays_dry_run_when_apply_flag_off(monkeypatch):
    monkeypatch.setenv("ENABLE_SCHEDULED_TITLE_REWRITE_APPLY", "0")
    jobs = digest.build_digest_jobs(SimpleNamespace(listing_channel="trading"))
    title = next(job for job in jobs if "标题" in job.label)
    assert title.enabled is True
    assert title.argv is not None
    assert "cro_title_rewrite.py" in " ".join(title.argv)
    assert "--apply" not in title.argv


def test_auto_publish_job_skips_when_no_ready_skus(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "ROOT", tmp_path)
    (tmp_path / "reports").mkdir()
    job = digest.build_mi_auto_publish_job(tmp_path)
    assert job.enabled is False
    assert "READY" in job.skip_reason
