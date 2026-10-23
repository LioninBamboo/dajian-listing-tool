"""Bug 1: autofix must pick today's live_audit, never a stale fat report."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

def _write_report(
    path: Path,
    *,
    mode: str = "live_audit",
    published: int = 217,
    generated_at: datetime | None = None,
    scope: str | None = "full",
    issues: list | None = None,
    mtime: float | None = None,
    corrupt: bool = False,
) -> Path:
    if corrupt:
        path.write_text("{not-json", encoding="utf-8")
    else:
        payload = {
            "mode": mode,
            "total_published": published,
            "issues": issues if issues is not None else [{"sku": "S1", "issues": [{"type": "x"}]}],
        }
        if generated_at is not None:
            payload["generated_at"] = generated_at.isoformat()
        if scope is not None:
            payload["scope"] = scope
        path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_pick_today_live_audit_over_old_fat_report(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    today = _write_report(
        tmp_path / "listing_audit_fix_20261022_113009.json",
        published=217,
        generated_at=now - timedelta(hours=0.7),
        mtime=(now - timedelta(hours=0.7)).timestamp(),
    )
    _write_report(
        tmp_path / "listing_audit_fix_20261016_115605.json",
        published=1000,
        generated_at=datetime(2026, 10, 16, 11, 56, 5),
        mtime=datetime(2026, 10, 16, 11, 56, 5).timestamp(),
    )
    picked = pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now)
    assert picked is not None
    assert picked[0] == today
    assert picked[1]["total_published"] == 217


def test_pick_live_audit_over_same_day_fix_output(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 13, 3, 0)
    live = _write_report(
        tmp_path / "listing_audit_fix_20261022_113009.json",
        mode="live_audit",
        published=217,
        generated_at=now - timedelta(hours=1.5),
        mtime=(now - timedelta(hours=1.5)).timestamp(),
    )
    _write_report(
        tmp_path / "listing_audit_fix_20261022_121000.json",
        mode="fix",
        published=78,
        generated_at=now - timedelta(minutes=10),
        mtime=(now - timedelta(minutes=10)).timestamp(),
    )
    picked = pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now)
    assert picked is not None
    assert picked[0] == live


def test_pick_yesterday_full_over_today_sku_scope(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    yesterday = _write_report(
        tmp_path / "listing_audit_fix_20261021_113000.json",
        published=202,
        generated_at=now - timedelta(hours=24),
        mtime=(now - timedelta(hours=24)).timestamp(),
    )
    _write_report(
        tmp_path / "listing_audit_fix_20261022_120000.json",
        published=1,
        scope="sku",
        generated_at=now - timedelta(minutes=5),
        mtime=(now - timedelta(minutes=5)).timestamp(),
    )
    # Same-day-only: yesterday is not accepted unless --allow-yesterday.
    assert pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now) is None
    picked = pick_latest_daily_live_audit(
        list(tmp_path.glob("*.json")), now=now, allow_yesterday=True
    )
    assert picked is not None
    assert picked[0] == yesterday


def test_stale_fat_report_is_not_picked(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    _write_report(
        tmp_path / "listing_audit_fix_20261016_115605.json",
        published=1000,
        generated_at=datetime(2026, 10, 16, 11, 56, 5),
        mtime=datetime(2026, 10, 16, 11, 56, 5).timestamp(),
    )
    assert pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now) is None


def test_legacy_unscoped_single_sku_is_ignored(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    _write_report(
        tmp_path / "listing_audit_fix_legacy.json",
        published=1,
        scope=None,
        generated_at=now - timedelta(hours=1),
        mtime=(now - timedelta(hours=1)).timestamp(),
    )
    assert pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now) is None


def test_corrupt_json_does_not_block_today_full(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    # Older corrupt file must not block a newer parseable same-day full report.
    _write_report(
        tmp_path / "listing_audit_fix_bad.json",
        corrupt=True,
        mtime=(now - timedelta(hours=3)).timestamp(),
    )
    today = _write_report(
        tmp_path / "listing_audit_fix_20261022_113009.json",
        published=217,
        generated_at=now - timedelta(hours=1),
        mtime=(now - timedelta(hours=1)).timestamp(),
    )
    picked = pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now)
    assert picked is not None
    assert picked[0] == today


def test_unparseable_generated_at_falls_back_to_mtime(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    path = tmp_path / "listing_audit_fix_bad_generated_at.json"
    path.write_text(
        json.dumps({
            "mode": "live_audit",
            "scope": "full",
            "total_published": 80,
            "generated_at": "not-a-date",
            "issues": [{"sku": "S1", "issues": [{"type": "x"}]}],
        }),
        encoding="utf-8",
    )
    os.utime(path, ((now - timedelta(hours=2)).timestamp(),) * 2)
    picked = pick_latest_daily_live_audit([path], now=now)
    assert picked is not None
    assert picked[0] == path


def test_missing_generated_at_falls_back_to_mtime(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    today = _write_report(
        tmp_path / "listing_audit_fix_no_generated_at.json",
        published=80,
        generated_at=None,
        mtime=(now - timedelta(hours=2)).timestamp(),
    )
    picked = pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now)
    assert picked is not None
    assert picked[0] == today


def test_empty_issues_are_ignored(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    _write_report(
        tmp_path / "listing_audit_fix_empty.json",
        published=217,
        generated_at=now - timedelta(hours=1),
        issues=[],
    )
    assert pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now) is None


def test_newest_empty_full_blocks_older_in_window_findings(tmp_path):
    """A fresh empty full live_audit must not lose to yesterday's findings."""
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    _write_report(
        tmp_path / "listing_audit_fix_20261022_113009.json",
        published=217,
        generated_at=now - timedelta(hours=1),
        issues=[],
        mtime=(now - timedelta(hours=1)).timestamp(),
    )
    _write_report(
        tmp_path / "listing_audit_fix_20261021_113000.json",
        published=202,
        generated_at=now - timedelta(hours=24),
        issues=[{"sku": "OLD1", "issues": [{"type": "semantic_material"}]}],
        mtime=(now - timedelta(hours=24)).timestamp(),
    )
    assert pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now) is None


def test_unreadable_mtime_is_skipped_when_generated_at_missing(tmp_path, monkeypatch):
    from scripts import semantic_rewrite as cli

    now = datetime(2026, 10, 22, 12, 10, 0)
    path = _write_report(
        tmp_path / "listing_audit_fix_unreadable.json",
        published=80,
        generated_at=None,
        mtime=(now - timedelta(hours=1)).timestamp(),
    )
    original_stat = Path.stat

    def boom(self, *args, **kwargs):
        if self == path:
            raise OSError("mtime unreadable")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    assert cli.pick_latest_daily_live_audit([path], now=now) is None


def test_autofix_tasks_skip_without_fresh_report(tmp_path, monkeypatch):
    import scheduler_daemon

    health = {}

    monkeypatch.setattr(scheduler_daemon, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(scheduler_daemon, "_task_succeeded_today", lambda name: False)
    monkeypatch.setattr(
        scheduler_daemon,
        "update_health",
        lambda name, status, message="", duration_sec=0.0: health.setdefault(name, (status, message)),
    )
    monkeypatch.setattr(
        scheduler_daemon,
        "run_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run autofix")),
    )
    (tmp_path / "logs").mkdir()
    _write_report(
        tmp_path / "logs" / "listing_audit_fix_20261016_115605.json",
        published=1000,
        generated_at=datetime(2026, 10, 16, 11, 56, 5),
        mtime=datetime(2026, 10, 16, 11, 56, 5).timestamp(),
    )

    ok, msg = scheduler_daemon.task_source_aspect_autofix()
    assert ok is True
    assert msg == "SKIP: no fresh daily live_audit"
    assert health["source_aspect_autofix"][0] == "skipped"
    assert "SKIP: no fresh daily live_audit" in health["source_aspect_autofix"][1]

    ok, msg = scheduler_daemon.task_missing_video_autofix()
    assert ok is True
    assert msg == "SKIP: no fresh daily live_audit"
    assert health["missing_video_autofix"][0] == "skipped"

    ok, msg = scheduler_daemon.task_semantic_rewrite()
    assert ok is True
    assert msg == "SKIP: no fresh daily live_audit"
    assert health["semantic_rewrite"][0] == "skipped"
    assert "SKIP: no fresh daily live_audit" in health["semantic_rewrite"][1]


def test_audit_report_scope_values():
    from scripts.audit_fix_active_listings import audit_report_scope

    assert audit_report_scope(type("A", (), {"sku": None, "sku_file": None, "source_report": None})()) == "full"
    assert audit_report_scope(type("A", (), {"sku": "X", "sku_file": None, "source_report": None})()) == "sku"
    assert audit_report_scope(type("A", (), {"sku": None, "sku_file": "f", "source_report": None})()) == "sku"
    assert audit_report_scope(type("A", (), {"sku": None, "sku_file": None, "source_report": "r.json"})()) == "source_report"


def test_derive_queue_uses_daily_selector(tmp_path, monkeypatch):
    from scripts import semantic_rewrite as cli

    now = datetime(2026, 10, 22, 12, 30, 0)
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_report(
        logs / "listing_audit_fix_20261022_113009.json",
        published=217,
        generated_at=now - timedelta(hours=1),
        issues=[{"sku": "TODAY1", "issues": [{"type": "semantic_material", "severity": "CRITICAL"}]}],
        mtime=(now - timedelta(hours=1)).timestamp(),
    )
    _write_report(
        logs / "listing_audit_fix_20261016_115605.json",
        published=1000,
        generated_at=datetime(2026, 10, 16, 11, 56, 5),
        issues=[{"sku": "OLD1", "issues": [{"type": "semantic_material", "severity": "CRITICAL"}]}],
        mtime=datetime(2026, 10, 16, 11, 56, 5).timestamp(),
    )
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "datetime", datetime)
    # Freeze "now" inside the picker via wrapper
    real = cli.pick_latest_daily_live_audit

    def _frozen(files, *, max_age_hours=36, now=None, allow_yesterday=False):
        return real(
            files,
            max_age_hours=max_age_hours,
            now=now or datetime(2026, 10, 22, 12, 30, 0),
            allow_yesterday=allow_yesterday,
        )

    monkeypatch.setattr(cli, "pick_latest_daily_live_audit", _frozen)
    queue = cli.derive_queue_from_latest_audit()
    assert queue == ["TODAY1"]
