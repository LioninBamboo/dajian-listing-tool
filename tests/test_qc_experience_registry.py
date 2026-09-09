import sqlite3

import pytest

from src.services.qc_experience_registry import (
    derive_rule_ids,
    derive_contextual_rule_ids,
    ensure_qc_experience_registry,
    register_experience,
    register_rule,
    resolve_rule_provenance,
)


def test_registry_records_experience_and_rule_provenance():
    conn = sqlite3.connect(":memory:")

    ensure_qc_experience_registry(conn)
    experience = register_experience(
        conn,
        experience_id="EXP-20260804-CAPACITY",
        title="Structured capacity overrides stale source copy",
        domain="fact_sheet",
        root_cause="source_copy_conflict",
        evidence={"source": "Seats=4 Seat", "copy": "5 people"},
        fix_action="keep structured capacity and remove unsupported copy",
        status="confirmed",
    )
    rule = register_rule(
        conn,
        rule_id="FS-CAPACITY-001",
        experience_id="EXP-20260804-CAPACITY",
        domain="fact_sheet",
        severity="HIGH",
        condition="structured capacity agrees while prose conflicts",
        action="block unsupported candidate claim",
        source_evidence_required=True,
        generator_path="listing_fact_sheet",
        test_path="tests/test_listing_fact_sheet.py",
        status="enforced",
    )

    provenance = resolve_rule_provenance(conn, ["FS-CAPACITY-001"])

    assert experience["experience_id"] == "EXP-20260804-CAPACITY"
    assert rule["rule_id"] == "FS-CAPACITY-001"
    assert provenance == [
        {
            "rule_id": "FS-CAPACITY-001",
            "experience_id": "EXP-20260804-CAPACITY",
            "domain": "fact_sheet",
            "severity": "HIGH",
            "status": "enforced",
            "test_path": "tests/test_listing_fact_sheet.py",
        }
    ]


def test_registry_rejects_rule_without_registered_experience():
    conn = sqlite3.connect(":memory:")
    ensure_qc_experience_registry(conn)

    with pytest.raises(ValueError, match="experience_id"):
        register_rule(
            conn,
            rule_id="FS-MISSING-001",
            experience_id="EXP-MISSING",
            domain="fact_sheet",
            severity="CRITICAL",
            condition="missing experience",
            action="block",
        )


def test_derive_rule_ids_is_deterministic_for_quality_and_fact_issues():
    rule_ids = derive_rule_ids(
        quality_issues=[{"code": "wrong_category"}, {"code": "wrong_category"}],
        fact_sheet_violations=[
            {"claim_type": "semantic_material"},
            {"claim_type": "semantic_capacity"},
        ],
    )

    assert rule_ids == [
        "QUALITY_GATE-WRONG_CATEGORY",
        "FACT_SHEET-SEMANTIC_MATERIAL",
        "FACT_SHEET-SEMANTIC_CAPACITY",
    ]


def test_contextual_rule_ids_keep_hall_tree_and_side_table_rules_narrow():
    hall_tree = derive_contextual_rule_ids(
        source_title="Farmhouse Wooden Hall Tree with Storage Bench",
        candidate_title="Farmhouse Wooden Hall Tree with 6 Hooks",
        quality_issues=[],
        fact_sheet_violations=[{"claim_type": "semantic_dimension"}],
    )
    side_table = derive_contextual_rule_ids(
        source_title="23 inch Mid-Century Side Table",
        candidate_title="Side Table with Woven Shelf",
        quality_issues=[{"code": "category_mismatch"}],
        fact_sheet_violations=[],
    )
    unrelated = derive_contextual_rule_ids(
        source_title="Modern Dining Table",
        candidate_title="Modern Dining Table",
        quality_issues=[{"code": "category_mismatch"}],
        fact_sheet_violations=[{"claim_type": "semantic_dimension"}],
    )

    assert hall_tree == ["QUALITY_GATE-HALL_TREE_DIMENSION_ORIENTATION"]
    assert side_table == ["QUALITY_GATE-SIDE_TABLE_CATEGORY_PROFILE"]
    assert unrelated == []
