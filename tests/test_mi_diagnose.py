import json
from datetime import datetime


def test_build_report_contains_required_sections(tmp_path):
    from scripts import mi_diagnose

    root = tmp_path
    reports = root / "reports"
    reports.mkdir()
    now = datetime(2026, 5, 2, 9, 40)
    today = now.strftime("%Y%m%d")

    (reports / f"mi_opportunities_{today}_090000.json").write_text(
        json.dumps({
            "opportunities": [{"sku": "SKU1"}],
            "by_category": [{"category": "sofa", "count": 1, "avg_score": 80}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (reports / f"mi_digest_{today}.html").write_text(
        f"<html><body><h2>市场情报 每日摘要 - 2026-05-02</h2><p>快照文件: <code>mi_opportunities_{today}_090000.json</code> · 共发现 <b>1</b> 条机会</p></body></html>",
        encoding="utf-8",
    )
    (reports / "mi_long_window_history.json").write_text(
        json.dumps([
            {"date": "2026-05-01", "trend_label": "stable", "drift": 0.0},
            {"date": "2026-05-02", "trend_label": "falling", "drift": -0.2},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    (reports / "mi_alerts_state.json").write_text(
        json.dumps({
            "persistent_falling_trend": {
                "last_sent_at": "2026-05-02T09:35:00",
                "severity": "high",
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (reports / "mi_blacklist.json").write_text(
        json.dumps({"SKU1": {"reason": "manual"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (root / "mi_categories.json").write_text(
        json.dumps({
            "categories": [{"label": "sofa", "keywords": ["sofa"]}],
            "fallback": "other",
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    report = mi_diagnose.build_report(project_root=root, now=now)

    assert report["snapshots"]["count"] == 1
    assert report["digests"]["count_last_7d"] == 1
    assert report["trend"]["latest_date"] == "2026-05-02"
    assert report["alerts"]["latest_last_sent_at"] == "2026-05-02T09:35:00"
    assert report["blacklist"]["active_count"] == 1
    assert report["category_rules"]["category_count"] == 1
    assert report["self_check"]["ok"] is True


def test_main_json_output(monkeypatch, capsys):
    from scripts import mi_diagnose

    fake_report = {
        "generated_at": "2026-05-02T09:40:00",
        "project_root": "C:/repo",
        "reports_dir": "C:/repo/reports",
        "snapshots": {"count": 0, "latest_name": None, "latest_opportunity_count": 0, "latest_by_category_count": 0, "top_category": None},
        "digests": {"count_last_7d": 0, "latest_names": []},
        "trend": {"exists": False, "entry_count": 0, "latest_date": None, "latest_trend_label": None, "recent": [], "persistent_falling": False},
        "alerts": {"exists": False, "type_count": 0, "latest_last_sent_at": None, "by_type": {}},
        "blacklist": {"exists": False, "active_count": 0, "reasons": {}},
        "category_rules": {"exists": False, "category_count": 0, "fallback": None, "mtime": None},
        "self_check": {"ok": False, "status": "fail", "severity": "high", "issues": ["x"], "details": {}},
    }
    monkeypatch.setattr(mi_diagnose, "build_report", lambda project_root=None, now=None: fake_report)

    exit_code = mi_diagnose.main(["--json"])
    captured = capsys.readouterr().out

    assert exit_code == 0
    payload = json.loads(captured)
    assert payload["self_check"]["severity"] == "high"
    assert payload["digests"]["count_last_7d"] == 0