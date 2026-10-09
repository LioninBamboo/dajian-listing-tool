import sqlite3

from src.services.qc_experience_registry import resolve_rule_provenance
from src.services.qc_experience_seed import (
    INITIAL_QC_EXPERIENCES,
    INITIAL_QC_RULES,
    seed_initial_qc_experiences,
)


def test_initial_qc_seed_contains_verified_cross_domain_cases():
    experience_ids = {item["experience_id"] for item in INITIAL_QC_EXPERIENCES}
    rule_ids = {item["rule_id"] for item in INITIAL_QC_RULES}

    assert {
        "EXP-CAPACITY-20260804",
        "EXP-HALL-TREE-DIMENSIONS-20260804",
        "EXP-SIDE-TABLE-CATEGORY-20260804",
        "EXP-INVENTORY-ARRIVAL-DATE-20260804",
        "EXP-INVENTORY-STOCK-RECONCILIATION-20260804",
        "EXP-PARTIAL-WRITE-20260804",
    } <= experience_ids
    assert {
        "FACT_SHEET-SEMANTIC_CAPACITY",
        "QUALITY_GATE-HALL_TREE_DIMENSION_ORIENTATION",
        "QUALITY_GATE-SIDE_TABLE_CATEGORY_PROFILE",
        "INVENTORY-OOS-ARRIVAL-DATE-001",
        "INVENTORY-GIGA-STOCK-EBAY-QTY-001",
        "PUBLISH-PARTIAL-WRITE-READBACK-001",
    } <= rule_ids


def test_seed_registers_cases_and_is_idempotent():
    conn = sqlite3.connect(":memory:")

    first = seed_initial_qc_experiences(conn)
    second = seed_initial_qc_experiences(conn)
    provenance = resolve_rule_provenance(
        conn,
        ["FACT_SHEET-SEMANTIC_CAPACITY", "PUBLISH-PARTIAL-WRITE-READBACK-001"],
    )

    assert first == {"experiences": 6, "rules": 6}
    assert second == first
    assert [item["experience_id"] for item in provenance] == [
        "EXP-CAPACITY-20260804",
        "EXP-PARTIAL-WRITE-20260804",
    ]
    assert all(item["test_path"] for item in provenance)

