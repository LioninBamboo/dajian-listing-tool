import daily_tasks


def test_run_ops_daily_calls_sync_and_ghost_recovery_only(monkeypatch):
    calls = []

    monkeypatch.setattr(daily_tasks, "sync_inventory", lambda: calls.append("sync") or {"checked_count": 1})
    monkeypatch.setattr(
        daily_tasks,
        "run_ghost_oos_recovery",
        lambda auto_fix=True: calls.append(("ghost", auto_fix)) or {"status": "ok", "qty_zero_restocked": []},
    )

    result = daily_tasks.run_ops_daily()

    assert calls == ["sync", ("ghost", True)]
    assert "inventory" in result
    assert result["inventory"]["full_oos_audit"]["status"] == "ok"


def test_main_ops_only_sends_ops_email(monkeypatch):
    sent = []

    monkeypatch.setattr(
        daily_tasks,
        "run_ops_daily",
        lambda: {"inventory": {"checked_count": 2, "full_oos_audit": {"checked_count": 3, "restocked_count": 1}}},
    )
    monkeypatch.setattr(daily_tasks, "send_ops_summary_email", lambda results: sent.append(results) or True)
    monkeypatch.setattr(daily_tasks, "send_daily_summary_email", lambda results: sent.append("daily"))
    monkeypatch.setattr(daily_tasks, "assert_runtime_not_in_maintenance", lambda *_a, **_k: None)
    monkeypatch.setattr(
        daily_tasks,
        "validate_runtime_database",
        lambda *_a, **_k: type("R", (), {"integrity_check": "ok", "link_count": 0, "page_count": 0})(),
    )
    monkeypatch.setattr(daily_tasks, "run_report_retention_cleanup", lambda: None)
    monkeypatch.setattr(
        daily_tasks.sys,
        "argv",
        ["daily_tasks.py", "--ops-only"],
    )

    daily_tasks.main()

    assert len(sent) == 1
    assert "inventory" in sent[0]
