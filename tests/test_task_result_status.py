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
