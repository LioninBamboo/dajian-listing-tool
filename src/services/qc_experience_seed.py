"""Initial, evidence-linked QC experiences from the 2026-08-04 repair round.

The seed is data only.  Calling ``seed_initial_qc_experiences`` is an explicit
local database action; importing this module never changes the database.
"""

from __future__ import annotations

from src.services.qc_experience_registry import (
    ensure_qc_experience_registry,
    register_experience,
    register_rule,
)


INITIAL_QC_EXPERIENCES = (
    {
        "experience_id": "EXP-CAPACITY-20260804",
        "title": "Structured capacity resolves stale prose conflict",
        "domain": "fact_sheet",
        "root_cause": "source_structured_copy_conflict",
        "evidence": {
            "report": "logs/critical_targeted_reaudit_after_recommended_fixes_20260804.json",
            "source_example": "Seats=4 Seat",
            "copy_example": "seats 5 people",
        },
        "fix_action": "Prefer explicit structured capacity and remove unsupported candidate prose.",
        "status": "confirmed",
        "ruleset_version": "listing-qc-v1",
    },
    {
        "experience_id": "EXP-HALL-TREE-DIMENSIONS-20260804",
        "title": "Hall Tree dimensions need product-aware orientation",
        "domain": "quality_gate",
        "root_cause": "dimension_axis_misclassification",
        "evidence": {
            "report": "logs/critical_manual_followup_results_20260804.json",
            "product_family": "hall tree",
            "risk": "title size class can be mistaken for item length",
        },
        "fix_action": "Normalize dimensions using the product profile and preserve explicit assembled measurements.",
        "status": "confirmed",
        "ruleset_version": "listing-qc-v1",
    },
    {
        "experience_id": "EXP-SIDE-TABLE-CATEGORY-20260804",
        "title": "Side Table must not inherit sofa-specific aspects",
        "domain": "quality_gate",
        "root_cause": "category_profile_mismatch",
        "evidence": {
            "report": "logs/critical_manual_followup_results_20260804.json",
            "target_category": "38204",
            "target_type": "End & Side Tables",
        },
        "fix_action": "Apply the side-table profile and remove upholstery/set-includes aspects.",
        "status": "confirmed",
        "ruleset_version": "listing-qc-v1",
    },
    {
        "experience_id": "EXP-INVENTORY-ARRIVAL-DATE-20260804",
        "title": "Out of stock with an arrival date can remain listed",
        "domain": "inventory",
        "root_cause": "oos_policy_missing_arrival_exception",
        "evidence": {
            "source_system": "GIGA",
            "policy": "preserve listing when a reliable arrival date exists",
        },
        "fix_action": "Route to inventory policy evaluation; do not end solely because current quantity is zero.",
        "status": "confirmed",
        "ruleset_version": "inventory-policy-v1",
    },
    {
        "experience_id": "EXP-INVENTORY-STOCK-RECONCILIATION-20260804",
        "title": "GIGA stock must not be hidden by an eBay zero quantity",
        "domain": "inventory",
        "root_cause": "cross_system_quantity_drift",
        "evidence": {
            "source_system": "GIGA/eBay",
            "policy": "verify GIGA stock before ending or deleting a listing",
        },
        "fix_action": "Hold end/delete action and reconcile inventory before making any live change.",
        "status": "confirmed",
        "ruleset_version": "inventory-policy-v1",
    },
    {
        "experience_id": "EXP-PARTIAL-WRITE-20260804",
        "title": "eBay error responses can still leave fields written",
        "domain": "publish_readback",
        "root_cause": "replacement_put_partial_write",
        "evidence": {
            "report": "logs/critical_manual_followup_results_20260804.json",
            "observed_behavior": "HTTP 400 followed by partially updated listing fields",
        },
        "fix_action": "Classify write state from Inventory/Offer readback before recording success or retrying.",
        "status": "confirmed",
        "ruleset_version": "listing-qc-v1",
    },
)


INITIAL_QC_RULES = (
    {
        "rule_id": "FACT_SHEET-SEMANTIC_CAPACITY",
        "experience_id": "EXP-CAPACITY-20260804",
        "domain": "fact_sheet",
        "severity": "HIGH",
        "condition": "structured capacity and candidate prose disagree",
        "action": "block unsupported claim or apply structured-field conflict resolution",
        "generator_path": "listing_fact_sheet",
        "test_path": "tests/test_listing_fact_sheet.py",
        "status": "enforced",
        "ruleset_version": "listing-qc-v1",
    },
    {
        "rule_id": "QUALITY_GATE-HALL_TREE_DIMENSION_ORIENTATION",
        "experience_id": "EXP-HALL-TREE-DIMENSIONS-20260804",
        "domain": "quality_gate",
        "severity": "HIGH",
        "condition": "hall-tree title dimensions conflict with assembled source dimensions",
        "action": "normalize product-aware dimension orientation",
        "generator_path": "listing_fact_sheet",
        "test_path": "tests/test_listing_fact_sheet.py",
        "status": "enforced",
        "ruleset_version": "listing-qc-v1",
    },
    {
        "rule_id": "QUALITY_GATE-SIDE_TABLE_CATEGORY_PROFILE",
        "experience_id": "EXP-SIDE-TABLE-CATEGORY-20260804",
        "domain": "quality_gate",
        "severity": "HIGH",
        "condition": "side-table family receives sofa-specific category/aspects",
        "action": "apply End & Side Tables profile and remove incompatible aspects",
        "generator_path": "listing_quality_gate",
        "test_path": "tests/test_listing_quality_gate.py",
        "status": "enforced",
        "ruleset_version": "listing-qc-v1",
    },
    {
        "rule_id": "INVENTORY-OOS-ARRIVAL-DATE-001",
        "experience_id": "EXP-INVENTORY-ARRIVAL-DATE-20260804",
        "domain": "inventory",
        "severity": "HIGH",
        "condition": "GIGA quantity is zero but a reliable arrival date exists",
        "action": "manual_review; preserve listing unless another hard blocker exists",
        "generator_path": "inventory_reconciliation",
        "test_path": "tests/test_listing_status_sync.py",
        "status": "manual_review",
        "ruleset_version": "inventory-policy-v1",
    },
    {
        "rule_id": "INVENTORY-GIGA-STOCK-EBAY-QTY-001",
        "experience_id": "EXP-INVENTORY-STOCK-RECONCILIATION-20260804",
        "domain": "inventory",
        "severity": "CRITICAL",
        "condition": "GIGA reports stock while eBay quantity is zero",
        "action": "manual_review; reconcile before end/delete or quantity change",
        "generator_path": "inventory_reconciliation",
        "test_path": "tests/test_listing_status_sync.py",
        "status": "manual_review",
        "ruleset_version": "inventory-policy-v1",
    },
    {
        "rule_id": "PUBLISH-PARTIAL-WRITE-READBACK-001",
        "experience_id": "EXP-PARTIAL-WRITE-20260804",
        "domain": "publish_readback",
        "severity": "CRITICAL",
        "condition": "eBay write response is an error or readback differs from the snapshot",
        "action": "stop batch and classify partial_write or transport_failure",
        "generator_path": "listing_publish_readback",
        "test_path": "tests/test_listing_publish_readback.py",
        "status": "enforced",
        "ruleset_version": "listing-qc-v1",
    },
)


def seed_initial_qc_experiences(conn, *, commit: bool = True) -> dict[str, int]:
    """Idempotently load the initial verified case/rule catalog into ``conn``."""

    ensure_qc_experience_registry(conn, commit=False)
    for experience in INITIAL_QC_EXPERIENCES:
        register_experience(conn, commit=False, **experience)
    for rule in INITIAL_QC_RULES:
        register_rule(conn, commit=False, source_evidence_required=True, **rule)
    if commit:
        conn.commit()
    return {"experiences": len(INITIAL_QC_EXPERIENCES), "rules": len(INITIAL_QC_RULES)}
