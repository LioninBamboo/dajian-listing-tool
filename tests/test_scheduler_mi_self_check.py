"""F32 — scheduler_daemon.check_mi_pipeline_health 单元测试。"""
import json
from datetime import datetime

import pytest


def _make_snapshot(reports, today_str):
    snap = reports / f"mi_opportunities_{today_str}_120000.json"
    snap.write_text(
        json.dumps({"opportunities": [], "by_category": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    return snap


def _make_digest(reports, today_str, snapshot_name=None, opportunity_count=0):
    snapshot_ref = snapshot_name or f"mi_opportunities_{today_str}_120000.json"
    (reports / f"mi_digest_{today_str}.html").write_text(
        (
            "<html><body>"
            f"<h2>市场情报 每日摘要 - {today_str[:4]}-{today_str[4:6]}-{today_str[6:8]}</h2>"
            f"<p>快照文件: <code>{snapshot_ref}</code> · 共发现 <b>{opportunity_count}</b> 条机会</p>"
            "</body></html>"
        ),
        encoding="utf-8",
    )


def _make_trend(reports, today_iso):
    (reports / "mi_long_window_history.json").write_text(
        json.dumps([{"date": today_iso, "trend_label": "stable"}]),
        encoding="utf-8",
    )


class TestF32MiPipelineSelfCheck:
    def test_ok_when_all_present(self, tmp_path):
        from scheduler_daemon import check_mi_pipeline_health
        reports = tmp_path / "reports"
        reports.mkdir()
        now = datetime(2026, 5, 2, 9, 35)
        today = now.strftime("%Y%m%d")
        snap = _make_snapshot(reports, today)
        _make_digest(reports, today, snapshot_name=snap.name, opportunity_count=0)
        _make_trend(reports, now.strftime("%Y-%m-%d"))

        result = check_mi_pipeline_health(reports_dir=reports, now=now)
        assert result["ok"] is True
        assert result["status"] == "ok"
        assert result["severity"] == "info"
        assert result["details"]["snapshots_today"] == 1
        assert result["details"]["digest_today_exists"] is True
        assert result["details"]["trend_latest_date"] == "2026-05-02"

    def test_flags_missing_snapshot_and_digest(self, tmp_path):
        from scheduler_daemon import check_mi_pipeline_health
        reports = tmp_path / "reports"
        reports.mkdir()
        now = datetime(2026, 5, 2, 9, 35)

        result = check_mi_pipeline_health(reports_dir=reports, now=now)
        assert result["ok"] is False
        assert result["severity"] == "high"
        joined = " | ".join(result["issues"])
        assert "无 MI 快照" in joined
        assert "日报归档缺失" in joined
        assert "trend 历史缺失" in joined

    def test_flags_stale_trend(self, tmp_path):
        from scheduler_daemon import check_mi_pipeline_health
        reports = tmp_path / "reports"
        reports.mkdir()
        now = datetime(2026, 5, 2, 9, 35)
        today = now.strftime("%Y%m%d")
        snap = _make_snapshot(reports, today)
        _make_digest(reports, today, snapshot_name=snap.name, opportunity_count=0)
        # 老旧 trend 条目
        _make_trend(reports, "2026-04-30")

        result = check_mi_pipeline_health(reports_dir=reports, now=now)
        assert result["ok"] is False
        assert any("非今日" in i for i in result["issues"])

    def test_flags_empty_trend_history(self, tmp_path):
        from scheduler_daemon import check_mi_pipeline_health
        reports = tmp_path / "reports"
        reports.mkdir()
        now = datetime(2026, 5, 2, 9, 35)
        today = now.strftime("%Y%m%d")
        snap = _make_snapshot(reports, today)
        _make_digest(reports, today, snapshot_name=snap.name, opportunity_count=0)
        (reports / "mi_long_window_history.json").write_text("[]", encoding="utf-8")

        result = check_mi_pipeline_health(reports_dir=reports, now=now)
        assert result["ok"] is False
        assert any("历史为空" in issue for issue in result["issues"])
        assert result["details"]["trend_entry_count"] == 0

    def test_flags_digest_that_does_not_reference_today_snapshot(self, tmp_path):
        from scheduler_daemon import check_mi_pipeline_health
        reports = tmp_path / "reports"
        reports.mkdir()
        now = datetime(2026, 5, 2, 9, 35)
        today = now.strftime("%Y%m%d")
        _make_snapshot(reports, today)
        (reports / f"mi_digest_{today}.html").write_text(
            "<html><body><h2>市场情报 每日摘要 - 2026-05-02</h2><p>快照文件: <code>mi_opportunities_x.json</code></p></body></html>",
            encoding="utf-8",
        )
        _make_trend(reports, now.strftime("%Y-%m-%d"))

        result = check_mi_pipeline_health(reports_dir=reports, now=now)
        assert result["ok"] is False
        assert any("未引用任何当日快照" in issue for issue in result["issues"])

    def test_skip_today_check_when_today_only_false(self, tmp_path):
        from scheduler_daemon import check_mi_pipeline_health
        reports = tmp_path / "reports"
        reports.mkdir()
        now = datetime(2026, 5, 2, 9, 35)
        # 完全没有任何文件，但 today_only=False 时不应报错
        result = check_mi_pipeline_health(reports_dir=reports, now=now, today_only=False)
        assert result["ok"] is True

    def test_handles_missing_reports_dir(self, tmp_path):
        from scheduler_daemon import check_mi_pipeline_health
        # 目录不存在
        result = check_mi_pipeline_health(reports_dir=tmp_path / "nonexistent",
                                            now=datetime(2026, 5, 2))
        assert result["ok"] is False
        assert result["severity"] == "high"
        assert any("目录不存在" in i for i in result["issues"])

    def test_task_waits_while_daily_tasks_still_running(self, tmp_path, monkeypatch):
        import scheduler_daemon

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 5, 15, 9, 46, 0)

        health_path = tmp_path / "_scheduler_health.json"
        health_path.write_text(
            json.dumps({
                "tasks": {
                    "daily_tasks": {
                        "status": "running",
                        "at": "2026-05-15T09:30:18",
                        "message": "Started: daily_tasks.py",
                    }
                }
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        called = {"check": 0}

        def fake_check(*args, **kwargs):
            called["check"] += 1
            return {"ok": False, "status": "fail", "severity": "high",
                    "issues": ["should not check yet"], "details": {}}

        monkeypatch.setattr(scheduler_daemon, "HEALTH_FILE", health_path)
        monkeypatch.setattr(scheduler_daemon, "datetime", FixedDateTime)
        monkeypatch.setattr(scheduler_daemon, "check_mi_pipeline_health", fake_check)

        ok, message = scheduler_daemon.task_mi_self_check()

        assert ok is True
        assert "daily_tasks" in message
        assert called["check"] == 0
        health = json.loads(health_path.read_text(encoding="utf-8"))
        assert health["mi_self_check"]["status"] == "pending"
        assert health["mi_self_check"]["details"]["daily_tasks_status"] == "running"
