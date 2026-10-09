from types import SimpleNamespace

from src.utils.inventory_audit_contract import (
    AUDIT_SCOPE_FULL_OOS,
    AUDIT_SCOPE_INCREMENTAL,
    audit_scope_label,
    summarize_incremental_sync_results,
)


def test_incremental_sync_summary_uses_canonical_inventory_audit_fields():
    results = [
        SimpleNamespace(action="no_change", sku="A", supplier_in_stock=True),
        SimpleNamespace(action="out_of_stock", sku="B", supplier_in_stock=False),
        SimpleNamespace(action="restocked", sku="C", supplier_in_stock=True),
        SimpleNamespace(action="error", sku="D", supplier_in_stock=None),
    ]

    summary = summarize_incremental_sync_results(
        results,
        scope_count=10,
        skipped_count=6,
    )

    assert summary["audit_scope"] == AUDIT_SCOPE_INCREMENTAL
    assert summary["checked_count"] == 4
    assert summary["qty_zero_count"] == 1
    assert summary["supplier_oos_count"] == 1
    assert summary["restocked_count"] == 1
    assert summary["error_count"] == 1
    assert summary["scope_count"] == 10
    assert summary["skipped_count"] == 6


def test_supplier_shortage_remains_visible_when_ebay_zero_write_fails():
    summary = summarize_incremental_sync_results([
        SimpleNamespace(
            action="error",
            sku="WRITE-FAIL",
            supplier_in_stock=False,
        )
    ])

    assert summary["qty_zero_count"] == 0
    assert summary["supplier_oos_count"] == 1
    assert summary["error_count"] == 1


def test_full_audit_scope_is_distinct_from_incremental_scope():
    assert AUDIT_SCOPE_FULL_OOS != AUDIT_SCOPE_INCREMENTAL


def test_audit_scope_labels_are_human_readable():
    assert "增量库存同步" in audit_scope_label(AUDIT_SCOPE_INCREMENTAL)
    assert "全量" in audit_scope_label(AUDIT_SCOPE_FULL_OOS)


def test_health_check_can_skip_quantity_audit_without_second_full_scan(monkeypatch):
    from scripts.sales_health_check import SalesHealthChecker

    checker = SalesHealthChecker()
    monkeypatch.setattr(
        checker,
        "_get_published_products",
        lambda: [{
            "sku": "SKU-HEALTH",
            "title": "Test",
            "listing_id": "LISTING-HEALTH",
            "image_url": "",
            "category_id": "",
            "total_cost": 10.0,
            "selling_price": 20.0,
        }],
    )
    monkeypatch.setattr(checker, "_fetch_performance", lambda: ({}, {}))
    monkeypatch.setattr(checker, "_load_market_cache", lambda: {})
    monkeypatch.setattr(checker, "_check_price_integrity", lambda: None)
    monkeypatch.setattr(checker, "_print_summary", lambda: None)
    monkeypatch.setattr(
        checker,
        "_check_quantity_integrity",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("quantity audit duplicated")),
    )

    report = checker.run(run_quantity_audit=False)

    assert report["quantity_audit"]["audit_scope"] == AUDIT_SCOPE_FULL_OOS
    assert report["quantity_audit"]["checked_count"] == 0


def test_daily_incremental_sync_returns_canonical_fields(monkeypatch):
    import daily_tasks

    class FakeSyncService:
        last_sync_scope_count = 1000
        last_sync_skipped_count = 871

        def __init__(self):
            self.last_dajian_connection_error = ""

        def get_published_products(self):
            return [{"sku": f"S-{index}"} for index in range(1000)]

        def test_dajian_connection(self):
            return True

        def sync_all(self, **_kwargs):
            return [
                SimpleNamespace(
                    action="no_change",
                    sku="S-1",
                    supplier_in_stock=True,
                    old_value="",
                    new_value="",
                    message="",
                ),
                SimpleNamespace(
                    action="out_of_stock",
                    sku="S-2",
                    supplier_in_stock=False,
                    old_value="有库存",
                    new_value="库存设为0",
                    message="供应商无货",
                ),
            ]

    monkeypatch.setattr(
        "src.plugins.inventory_sync.sync_service.InventorySyncService",
        FakeSyncService,
    )

    result = daily_tasks.sync_inventory()

    assert result["audit_scope"] == AUDIT_SCOPE_INCREMENTAL
    assert result["checked_count"] == 2
    assert result["qty_zero_count"] == 1
    assert result["supplier_oos_count"] == 1
    assert result["restocked_count"] == 0
    assert result["error_count"] == 0
    assert result["scope_count"] == 1000
    assert result["skipped_count"] == 871
    assert result["checked"] == result["checked_count"]


def test_daily_summary_separates_incremental_and_full_audits(monkeypatch):
    import daily_tasks

    captured = {}
    monkeypatch.setattr(
        "src.utils.email_sender.send_email",
        lambda subject, html, **kwargs: captured.update(subject=subject, html=html) or True,
    )
    monkeypatch.setattr(daily_tasks, "get_store_profile", lambda: SimpleNamespace(brand_name="Test"))
    monkeypatch.setattr(daily_tasks, "_load_product_thumbnails", lambda skus: {})
    monkeypatch.setattr(daily_tasks, "build_thumbnail_img_html", lambda *args, **kwargs: "")

    ok = daily_tasks.send_daily_summary_email(
        {
            "analyze": {"success": 0, "failed": 0},
            "inventory": {
                "audit_scope": AUDIT_SCOPE_INCREMENTAL,
                "checked_count": 129,
                "qty_zero_count": 2,
                "supplier_oos_count": 2,
                "restocked_count": 1,
                "error_count": 3,
                "scope_count": 1000,
                "skipped_count": 871,
                "out_of_stock": ["B1", "B2"],
                "price_changed": [],
                "supplier_oos_skus": ["B1", "B2"],
                "full_oos_audit": {
                    "audit_scope": AUDIT_SCOPE_FULL_OOS,
                    "checked_count": 1000,
                    "qty_zero_count": 5,
                    "supplier_oos_count": 3,
                    "restocked_count": 2,
                    "error_count": 1,
                    "qty_zero_skus": ["Z1"],
                    "supplier_oos_skus": ["Z2"],
                    "restocked_items": [],
                },
            },
            "health_check": {},
            "smart_reprice": {"status": "scheduled_skip", "summary": {}, "changed_rows": []},
            "cro": {"summary": {}},
        }
    )

    assert ok is True
    html = captured["html"]
    assert "增量库存同步" in html
    assert "全量缺货审核" in html
    assert "129" in html
    assert "1000" in html
    assert "总检查" not in html


def test_daily_summary_keeps_both_audit_sections_when_incremental_sync_fails(monkeypatch):
    import daily_tasks

    captured = {}
    monkeypatch.setattr(
        "src.utils.email_sender.send_email",
        lambda subject, html, **kwargs: captured.update(subject=subject, html=html) or True,
    )
    monkeypatch.setattr(daily_tasks, "get_store_profile", lambda: SimpleNamespace(brand_name="Test"))
    monkeypatch.setattr(daily_tasks, "_load_product_thumbnails", lambda skus: {})
    monkeypatch.setattr(daily_tasks, "build_thumbnail_img_html", lambda *args, **kwargs: "")

    assert daily_tasks.send_daily_summary_email({
        "analyze": {"success": 0, "failed": 0},
        "inventory": {
            "audit_scope": AUDIT_SCOPE_INCREMENTAL,
            "checked_count": 0,
            "error_count": 1,
            "error": "大建 API 连接失败",
            "full_oos_audit": {
                "audit_scope": AUDIT_SCOPE_FULL_OOS,
                "checked_count": 1000,
                "qty_zero_count": 4,
                "supplier_oos_count": 2,
                "restocked_count": 1,
                "error_count": 0,
            },
        },
        "health_check": {},
        "smart_reprice": {"status": "scheduled_skip", "summary": {}, "changed_rows": []},
        "cro": {"summary": {}},
    }) is True

    assert "增量库存同步" in captured["html"]
    assert "全量缺货审核" in captured["html"]
    assert "大建 API 连接失败" in captured["html"]
