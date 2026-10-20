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


def test_summary_emails_keep_smtp_without_combined_digest(monkeypatch, tmp_path):
    """日报 / ops / MI 直接 SMTP。11:45 只巡检 listing_audit，没有合并摘要。"""
    from types import SimpleNamespace

    smtp_flags = []

    def fake_send(subject, html, **kwargs):
        smtp_flags.append(kwargs.get("send_smtp", True))
        return True

    monkeypatch.setattr("src.utils.email_sender.send_email", fake_send)
    monkeypatch.setattr(daily_tasks, "send_email", fake_send)
    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(daily_tasks, "get_store_profile", lambda: SimpleNamespace(brand_name="Test"))
    monkeypatch.setattr(daily_tasks, "_load_product_thumbnails", lambda skus: {})
    monkeypatch.setattr(daily_tasks, "build_thumbnail_img_html", lambda *a, **k: "")
    monkeypatch.setattr(daily_tasks, "_archive_mi_digest_html", lambda *a, **k: None)

    assert daily_tasks.send_daily_summary_email({
        "analyze": {"success": 0, "failed": 2},
        "inventory": {"checked_count": 1, "ghost_restock_error": "db locked"},
        "health_check": {"summary": {"problems": 3}, "email_html": "<p>health</p>"},
        "smart_reprice": {"status": "scheduled_skip", "summary": {}, "changed_rows": []},
        "cro": {"error": "traffic down"},
        "listing_audit": {"error": "audit blew up"},
    }) is True
    assert daily_tasks.send_ops_summary_email({
        "inventory": {"checked_count": 1, "ghost_restock_error": "db locked"},
    }) is True
    assert daily_tasks._send_mi_daily_digest(
        [{
            "sku": "A1",
            "title": "Sofa",
            "opportunity_score": 80,
            "suggested_price": 100,
            "margin_rate": 10,
            "potential_profit": 10,
            "seller_str_pct": 1,
            "image_url": "https://img.example.com/a.jpg",
        }],
        {"current_count": 1},
        "snap.json",
    ) is True
    assert smtp_flags
    assert all(flag is True for flag in smtp_flags)
