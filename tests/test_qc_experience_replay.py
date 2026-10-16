import json
import sqlite3

from src.services.qc_experience_replay import (
    build_historical_replay_report,
    build_experience_recurrence_report,
    replay_inventory_policy,
    replay_partial_write_fixture,
)
from src.services.qc_experience_seed import seed_initial_qc_experiences


def _seed_runtime_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE collected_products (id INTEGER PRIMARY KEY)")
        seed_initial_qc_experiences(conn)


def test_inventory_replay_preserves_arrival_date_and_holds_quantity_drift():
    arrival_case = replay_inventory_policy(
        giga_quantity=0,
        arrival_date="2026-08-15",
        ebay_quantity=0,
    )
    stock_drift_case = replay_inventory_policy(
        giga_quantity=3,
        arrival_date=None,
        ebay_quantity=0,
    )

    assert arrival_case == {
        "status": "manual_review",
        "action": "preserve_listing",
        "reason": "giga_oos_with_arrival_date",
    }
    assert stock_drift_case == {
        "status": "manual_review",
        "action": "hold_end_and_reconcile",
        "reason": "giga_stock_ebay_zero",
    }


def test_partial_write_replay_uses_readback_classifier():
    result = replay_partial_write_fixture()

    assert result["status"] == "partial_write"
    assert result["passed"] is False
    assert "image_urls_collapsed" in result["issue_codes"]
    assert result["clean_control_status"] == "verified"


def test_historical_replay_reports_rule_provenance_and_clean_controls(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    _seed_runtime_db(db_path)
    historical_path = tmp_path / "historical.json"
    clean_path = tmp_path / "clean.json"
    historical_path.write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "sku": "SKU-CAPACITY",
                        "issues": [{"type": "semantic_capacity", "severity": "CRITICAL"}],
                    },
                    {
                        "sku": "SKU-SIDE-TABLE",
                        "issues": [{"type": "category_mismatch", "severity": "CRITICAL"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    clean_path.write_text(
        json.dumps({"results": [{"sku": "SKU-CLEAN", "critical": []}]}),
        encoding="utf-8",
    )

    report = build_historical_replay_report(
        db_path=db_path,
        historical_paths=[historical_path],
        clean_paths=[clean_path],
    )

    by_case = {case["case_id"]: case for case in report["cases"]}
    assert report["summary"]["rule_provenance_complete"] is True
    assert report["summary"]["historical_evidence_cases"] == 2
    assert by_case["REPLAY-CAPACITY-20260804"]["replay_status"] == "blocked"
    assert by_case["REPLAY-SIDE-TABLE-20260804"]["historical_match_count"] == 1
    assert by_case["REPLAY-CAPACITY-20260804"]["clean_control_match_count"] == 0
    assert by_case["REPLAY-INVENTORY-ARRIVAL-DATE-20260804"]["replay_status"] == "manual_review"
    assert by_case["REPLAY-PARTIAL-WRITE-20260804"]["replay_status"] == "partial_write"


def test_recurrence_report_separates_clean_controls_from_missing_inventory_evidence(tmp_path):
    db_path = tmp_path / "ebay_collection.db"
    _seed_runtime_db(db_path)
    historical_path = tmp_path / "historical.json"
    clean_path = tmp_path / "clean.json"
    historical_path.write_text(
        json.dumps(
            {
                "issues": [
                    {
                        "sku": "SKU-CAPACITY",
                        "issues": [{"type": "semantic_capacity", "severity": "CRITICAL"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    clean_path.write_text(
        json.dumps({"results": [{"sku": "SKU-CLEAN", "critical": []}]}),
        encoding="utf-8",
    )

    report = build_experience_recurrence_report(
        db_path=db_path,
        historical_paths=[historical_path],
        clean_paths=[clean_path],
    )
    by_rule = {row["rule_id"]: row for row in report["rules"]}

    assert by_rule["FACT_SHEET-SEMANTIC_CAPACITY"]["historical_issue_count"] == 1
    assert by_rule["FACT_SHEET-SEMANTIC_CAPACITY"]["clean_control_issue_count"] == 0
    assert by_rule["FACT_SHEET-SEMANTIC_CAPACITY"]["triage"] == "keep_enforced"
    assert by_rule["INVENTORY-OOS-ARRIVAL-DATE-001"]["triage"] == "collect_cross_system_evidence"
    assert report["summary"]["rules_with_evidence_gap"] == 3
