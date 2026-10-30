from datetime import datetime

from src.utils.report_retention import cleanup_runtime_artifacts, find_expired_artifacts


def _write(root, relative_path, content="artifact"):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_find_expired_artifacts_uses_specific_rules_and_retention_tiers(tmp_path):
    _write(tmp_path, "reports/daily_report_20260801_010000.html")
    _write(tmp_path, "reports/terapeak_report_20260101.json")
    _write(tmp_path, "reports/critical_remediation_20260101.md")
    _write(tmp_path, "reports/mystery_20260101.json")
    _write(tmp_path, "reports/mi_blacklist.json")
    _write(tmp_path, "logs/daily_tasks_20260101_010000.log")
    _write(tmp_path, "logs/critical_audit_20260101.log")

    candidates = find_expired_artifacts(
        project_root=tmp_path,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )

    by_name = {candidate.path.name: candidate for candidate in candidates}
    assert set(by_name) == {
        "daily_report_20260801_010000.html",
        "terapeak_report_20260101.json",
        "critical_remediation_20260101.md",
        "mystery_20260101.json",
        "daily_tasks_20260101_010000.log",
        "critical_audit_20260101.log",
    }
    assert by_name["daily_report_20260801_010000.html"].keep_days == 3
    assert by_name["terapeak_report_20260101.json"].keep_days == 30
    assert by_name["critical_remediation_20260101.md"].keep_days == 180
    assert by_name["mystery_20260101.json"].keep_days == 30
    assert by_name["daily_tasks_20260101_010000.log"].keep_days == 30
    assert by_name["critical_audit_20260101.log"].keep_days == 180


def test_dated_runtime_fallback_excludes_databases_and_images(tmp_path):
    _write(tmp_path, "reports/unclassified_20260101.json")
    _write(tmp_path, "logs/unclassified_20260101.log")
    _write(tmp_path, "logs/database_backup_20260101.db")
    _write(tmp_path, "logs/screenshot_20260101.png")
    _write(tmp_path, "logs/critical_recovery_20260101.db")
    _write(tmp_path, "reports/critical_image_restore_20260101.png")
    _write(tmp_path, "logs/worker_state_20260101.json")
    _write(tmp_path, "reports/scheduled_tasks_snapshot_20260101.xml")

    candidates = find_expired_artifacts(
        project_root=tmp_path,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )

    assert {candidate.path.name for candidate in candidates} == {
        "unclassified_20260101.json",
        "unclassified_20260101.log",
    }


def test_protected_names_are_excluded_even_when_the_prefix_is_critical(tmp_path):
    _write(tmp_path, "logs/critical_state_20260101.log")
    _write(tmp_path, "logs/critical_recovery_20260101.db")
    _write(tmp_path, "logs/critical_snapshot_20260101.log")

    candidates = find_expired_artifacts(
        project_root=tmp_path,
        now=datetime(2026, 8, 12, 12, 0, 0),
    )

    assert candidates == []


def test_cleanup_runtime_artifacts_dry_run_then_apply(tmp_path):
    old_path = _write(tmp_path, "reports/terapeak_report_20260101.json")
    recent_path = _write(tmp_path, "reports/terapeak_report_20260812.json")

    dry_run = cleanup_runtime_artifacts(
        project_root=tmp_path,
        now=datetime(2026, 8, 12, 12, 0, 0),
        dry_run=True,
    )
    assert dry_run["candidate_count"] == 1
    assert dry_run["removed_count"] == 0
    assert old_path.exists()
    assert recent_path.exists()

    applied = cleanup_runtime_artifacts(
        project_root=tmp_path,
        now=datetime(2026, 8, 12, 12, 0, 0),
        dry_run=False,
    )
    assert applied["candidate_count"] == 1
    assert applied["removed_count"] == 1
    assert applied["errors"] == []
    assert not old_path.exists()
    assert recent_path.exists()


def test_daily_tasks_retention_wrapper_logs_and_returns_summary(monkeypatch):
    import daily_tasks

    expected = {
        "dry_run": False,
        "candidate_count": 2,
        "removed_count": 2,
        "error_count": 0,
        "errors": [],
        "candidates": [],
        "removed": [],
    }
    monkeypatch.setattr(
        daily_tasks,
        "cleanup_runtime_artifacts",
        lambda **kwargs: expected,
    )

    result = daily_tasks.run_report_retention_cleanup()

    assert result == expected
