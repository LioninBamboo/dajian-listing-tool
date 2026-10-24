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
