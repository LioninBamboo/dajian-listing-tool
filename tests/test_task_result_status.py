from src.utils.task_result_status import has_task_failure


def test_nested_error_marks_task_result_failed():
    results = {
        "inventory": {
            "synced": 0,
            "ghost_restock_error": "database disk image is malformed",
        }
    }

    assert has_task_failure(results) is True


def test_success_counts_and_scheduled_skip_do_not_mark_failure():
    results = {
        "inventory": {"success": 42, "failed": 0, "errors": []},
        "smart_reprice": {"status": "scheduled_skip", "changed_rows": []},
    }

    assert has_task_failure(results) is False


def test_explicit_failed_status_marks_task_result_failed():
    assert has_task_failure({"listing_audit": {"status": "timeout"}}) is True


def test_empty_error_fields_do_not_mark_failure():
    assert has_task_failure({"error": None, "nested": {"last_error": ""}}) is False


class TestInventoryItemLevelErrors:
    """2026-07-22: 库存同步 873 条里 18 条逐 SKU 报错,把整个 daily_tasks 判成
    failed、天天发❌告警,真失败被噪声淹没。与改价同属"完成但有条目级失败"。"""

    def test_inventory_item_errors_are_partial_not_failed(self):
        from src.utils.task_result_status import classify_daily_task_outcome
        results = {"inventory": {"checked": 873, "errors": 18, "no_change": 748},
                   "listing_audit": {"source": "live_ebay", "errors": 0}}
        assert classify_daily_task_outcome(results) == "partial_success"

    def test_inventory_zero_errors_is_success(self):
        from src.utils.task_result_status import classify_daily_task_outcome
        assert classify_daily_task_outcome({"inventory": {"checked": 873, "errors": 0}}) == "success"

    def test_real_task_failure_still_failed(self):
        from src.utils.task_result_status import classify_daily_task_outcome
        results = {"inventory": {"checked": 873, "errors": 18},
                   "some_task": {"status": "failed"}}
        assert classify_daily_task_outcome(results) == "failed"

    def test_inventory_never_ran_still_failed(self):
        from src.utils.task_result_status import classify_daily_task_outcome
        assert classify_daily_task_outcome({"inventory": {"checked": 0, "errors": 3}}) == "failed"

    def test_nested_full_audit_item_errors_are_partial(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        results = {
            "inventory": {
                "audit_scope": "incremental_inventory_sync",
                "checked_count": 129,
                "error_count": 0,
                "full_oos_audit": {
                    "audit_scope": "full_oos_audit",
                    "checked_count": 1000,
                    "error_count": 2,
                },
            }
        }
        assert classify_daily_task_outcome(results) == "partial_success"

    def test_nested_full_audit_error_without_checked_scope_still_failed(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        results = {
            "inventory": {
                "checked_count": 129,
                "error_count": 0,
                "full_oos_audit": {
                    "checked_count": 0,
                    "error_count": 1,
                },
            }
        }
        assert classify_daily_task_outcome(results) == "failed"


class TestMiSnapshotAutoPrepareItemLevel:
    """2026-11-18: MI 快照+digest 已落地, 但 auto_prepare 3 条草稿被质检拦住,
    nested ``failed`` 把整个 ``--mi-only`` 判成 failed, 健康面板误报链路故障。"""

    _SNAPSHOT_OK = {
        "status": "ok",
        "opportunities": 30,
        "snapshot": "mi_opportunities_20261118_104501.json",
        "auto_prepared": {
            "success": 0,
            "failed": 3,
            "skipped": 1,
            "requested": 4,
            "matched": 4,
            "prepared_skus": [],
        },
    }

    def test_qc_blocked_drafts_are_partial_not_failed(self):
        from src.utils.task_result_status import (
            classify_daily_task_outcome,
            exit_code_for_task_results,
        )

        results = {"mi_snapshot": dict(self._SNAPSHOT_OK)}
        assert classify_daily_task_outcome(results) == "partial_success"
        assert exit_code_for_task_results(results) == 2

    def test_missing_snapshot_still_failed(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        payload = dict(self._SNAPSHOT_OK)
        payload["snapshot"] = None
        assert classify_daily_task_outcome({"mi_snapshot": payload}) == "failed"

    def test_snapshot_error_still_failed(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        results = {
            "mi_snapshot": {
                **self._SNAPSHOT_OK,
                "error": "disk full",
            }
        }
        assert classify_daily_task_outcome(results) == "failed"

    def test_clean_auto_prepare_is_success(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        results = {
            "mi_snapshot": {
                **self._SNAPSHOT_OK,
                "auto_prepared": {
                    "success": 4,
                    "failed": 0,
                    "requested": 4,
                    "matched": 4,
                    "prepared_skus": ["A", "B", "C", "D"],
                },
            }
        }
        assert classify_daily_task_outcome(results) == "success"


class TestFinanceDegradedSkip:
    def test_finance_degraded_skip_does_not_fail_daily_tasks(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        results = {
            "inventory": {"checked_count": 10, "error_count": 0},
            "finance": {
                "status": "degraded_skipped",
                "reason": "HTTPSConnectionPool timed out",
            },
        }
        assert classify_daily_task_outcome(results) == "success"

    def test_finance_error_key_still_failed(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        results = {
            "inventory": {"checked_count": 10, "error_count": 0},
            "finance": {"error": "HTTPSConnectionPool timed out"},
        }
        assert classify_daily_task_outcome(results) == "failed"


class TestRepriceInsightsFallback:
    """2026-11-19: catalog reprice finished (264 writes, summary.errors=0) but
    nested marketplace_insights_access.error 403 failed the whole daily_tasks
    run. Browse fallback already priced the catalog."""

    _REPRICE_OK = {
        "status": "ok",
        "summary": {"total": 1470, "price_changes": 264, "errors": 0},
        "marketplace_insights_access": {
            "error": "Insufficient permissions to fulfill the request.",
        },
        "market_research": {
            "79691": {"error": "Insufficient permissions to fulfill the request."},
        },
    }

    def test_insights_403_after_completed_reprice_is_success(self):
        from src.utils.task_result_status import (
            classify_daily_task_outcome,
            exit_code_for_task_results,
        )

        results = {"smart_reprice": dict(self._REPRICE_OK)}
        assert classify_daily_task_outcome(results) == "success"
        assert exit_code_for_task_results(results) == 0

    def test_insights_403_plus_sku_write_errors_is_partial(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        payload = dict(self._REPRICE_OK)
        payload["summary"] = {"total": 1470, "price_changes": 264, "errors": 3}
        assert classify_daily_task_outcome({"smart_reprice": payload}) == "partial_success"

    def test_reprice_top_level_error_still_failed(self):
        from src.utils.task_result_status import classify_daily_task_outcome

        assert classify_daily_task_outcome(
            {"smart_reprice": {"status": "error", "error": "token expired"}}
        ) == "failed"
