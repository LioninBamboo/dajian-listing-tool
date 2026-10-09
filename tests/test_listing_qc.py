import sqlite3

import pytest

from src.services import listing_qc
from src.utils.listing_quality_gate import ListingQualityIssue
from src.services.qc_experience_registry import (
    ensure_qc_experience_registry,
    register_experience,
    register_rule,
)
from src.services.qc_experience_seed import seed_initial_qc_experiences


def _candidate():
    return {
        "title": "Solid Wood Side Table",
        "description": "<div><h2>KEY FEATURES</h2><ul><li>Solid wood side table.</li></ul></div>",
        "categoryId": "38204",
        "categoryName": "End & Side Tables",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["End & Side Tables"],
        },
    }


def _fact_sheet_result(status="pass", violations=None):
    return {
        "status": status,
        "violations": violations or [],
        "source_fingerprint": "source-hash",
        "candidate_fingerprint": "candidate-hash",
    }


def test_run_listing_qc_returns_pass_with_shared_inputs(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: _fact_sheet_result(),
    )

    result = listing_qc.run_listing_qc(
        sku="SKU-PASS",
        candidate=_candidate(),
        source_title="Solid Wood Side Table",
        source_description="A solid wood side table.",
        source_attributes={"Material": "Solid Wood"},
        source_specs={},
        images=["one", "two"],
        videos=[],
        fact_sheet_conn=sqlite3.connect(":memory:"),
    )

    assert result["status"] == "pass"
    assert result["fact_sheet_status"] == "pass"
    assert result["blockers"] == []
    assert result["source_fingerprint"] == "source-hash"
    assert result["candidate_fingerprint"] == "candidate-hash"
    assert result["sku"] == "SKU-PASS"


def test_run_listing_qc_blocks_fact_sheet_violation(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: _fact_sheet_result(
            "violations",
            [
                {
                    "claim_type": "semantic_material",
                    "claim_text": "fabric",
                    "severity": "CRITICAL",
                    "source_evidence": "solid wood",
                }
            ],
        ),
    )

    result = listing_qc.run_listing_qc(
        sku="SKU-FACT",
        candidate=_candidate(),
        source_title="Solid Wood Side Table",
        source_description="A solid wood side table.",
        source_attributes={"Material": "Solid Wood"},
        source_specs={},
        images=["one", "two"],
        fact_sheet_conn=sqlite3.connect(":memory:"),
    )

    assert result["status"] == "blocked"
    assert result["fact_sheet_status"] == "violations"
    assert len(result["blockers"]) == 1
    assert "semantic_material" in result["blockers"][0]
    assert result["fact_sheet_violations"][0]["severity"] == "CRITICAL"


def test_run_listing_qc_attaches_rule_and_experience_provenance(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: _fact_sheet_result(
            "violations",
            [
                {
                    "claim_type": "semantic_material",
                    "claim_text": "fabric",
                    "severity": "CRITICAL",
                    "source_evidence": "solid wood",
                }
            ],
        ),
    )
    conn = sqlite3.connect(":memory:")
    ensure_qc_experience_registry(conn)
    register_experience(
        conn,
        experience_id="EXP-MATERIAL-001",
        title="Unsupported material claim",
        domain="fact_sheet",
        root_cause="generator_hallucination",
        evidence={"source": "solid wood", "candidate": "fabric"},
        fix_action="remove unsupported material",
        status="confirmed",
    )
    register_rule(
        conn,
        rule_id="FACT_SHEET-SEMANTIC_MATERIAL",
        experience_id="EXP-MATERIAL-001",
        domain="fact_sheet",
        severity="CRITICAL",
        condition="candidate material is unsupported by source",
        action="block",
        status="enforced",
    )

    result = listing_qc.run_listing_qc(
        sku="SKU-PROVENANCE",
        candidate=_candidate(),
        source_title="Solid Wood Side Table",
        source_description="A solid wood side table.",
        source_attributes={"Material": "Solid Wood"},
        source_specs={},
        images=["one", "two"],
        fact_sheet_conn=conn,
    )

    assert result["rule_ids"] == ["FACT_SHEET-SEMANTIC_MATERIAL"]
    assert result["experience_ids"] == ["EXP-MATERIAL-001"]
    assert result["rule_provenance"][0]["status"] == "enforced"
    assert result["evidence_refs"][0]["source_evidence"] == "solid wood"


def test_run_listing_qc_attaches_narrow_side_table_rule_provenance(monkeypatch):
    monkeypatch.setattr(
        listing_qc,
        "validate_listing_quality",
        lambda *args, **kwargs: [
            ListingQualityIssue(
                "category_mismatch",
                "side_table should use category 38204",
                field="categoryId",
            )
        ],
    )
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: _fact_sheet_result(),
    )
    conn = sqlite3.connect(":memory:")
    seed_initial_qc_experiences(conn)

    result = listing_qc.run_listing_qc(
        sku="SKU-SIDE-TABLE",
        candidate=_candidate(),
        source_title="23 inch Mid-Century Side Table",
        source_description="A side table.",
        source_attributes={},
        source_specs={},
        images=["one", "two"],
        fact_sheet_conn=conn,
    )

    assert result["status"] == "blocked"
    assert result["rule_ids"] == [
        "QUALITY_GATE-CATEGORY_MISMATCH",
        "QUALITY_GATE-SIDE_TABLE_CATEGORY_PROFILE",
    ]
    assert "EXP-SIDE-TABLE-CATEGORY-20260804" in result["experience_ids"]


def test_run_listing_qc_marks_fact_sheet_unavailable_and_blocks(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: _fact_sheet_result("unavailable"),
    )

    result = listing_qc.run_listing_qc(
        sku="SKU-UNAVAILABLE",
        candidate=_candidate(),
        source_title="Solid Wood Side Table",
        source_description="A solid wood side table.",
        source_attributes={"Material": "Solid Wood"},
        source_specs={},
        images=["one", "two"],
        fact_sheet_conn=sqlite3.connect(":memory:"),
    )

    assert result["status"] == "unavailable"
    assert result["fact_sheet_status"] == "unavailable"
    assert result["blockers"] == ["FactSheet guard unavailable (LLM failed or disabled)"]


def test_run_listing_qc_does_not_skip_fact_sheet_when_connection_is_missing(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])

    result = listing_qc.run_listing_qc(
        sku="SKU-NO-CONN",
        candidate=_candidate(),
        source_title="Solid Wood Side Table",
        source_description="A solid wood side table.",
        source_attributes={"Material": "Solid Wood"},
        source_specs={},
        images=["one", "two"],
    )

    assert result["status"] == "unavailable"
    assert result["fact_sheet_status"] == "unavailable"
    assert result["blockers"]


def test_run_listing_qc_blocks_quality_gate_exception(monkeypatch):
    def fail_quality(*args, **kwargs):
        raise RuntimeError("quality rules unavailable")

    monkeypatch.setattr(listing_qc, "validate_listing_quality", fail_quality)

    result = listing_qc.run_listing_qc(
        sku="SKU-ERROR",
        candidate=_candidate(),
        source_title="Solid Wood Side Table",
        source_description="A solid wood side table.",
        source_attributes={"Material": "Solid Wood"},
        source_specs={},
        images=["one", "two"],
        fact_sheet_conn=sqlite3.connect(":memory:"),
    )

    assert result["status"] == "blocked"
    assert result["fact_sheet_status"] == "not_run"
    assert result["blockers"] == ["Quality gate error: quality rules unavailable"]


def test_motors_profile_ignores_furniture_fact_sheet(monkeypatch):
    called = {"quality": False, "fact": False}

    def boom_quality(*args, **kwargs):
        called["quality"] = True
        raise RuntimeError("furniture gate must not run")

    def boom_fact(**kwargs):
        called["fact"] = True
        return _fact_sheet_result(
            "violations",
            [
                {
                    "claim_type": "semantic_material",
                    "claim_text": "polyurethane",
                    "severity": "CRITICAL",
                    "source_evidence": "pu, steel",
                }
            ],
        )

    monkeypatch.setattr(listing_qc, "validate_listing_quality", boom_quality)
    monkeypatch.setattr(listing_qc, "check_fact_sheet_violations", boom_fact)

    result = listing_qc.run_listing_qc(
        sku="W465P220507",
        candidate={
            "title": 'Air Compressor Accessory Kit - 3/8" OD x 60 Ft Blue PU Tubing',
            "description": "PU air hose kit.",
            "categoryId": "173950",
            "categoryName": "Air Compressors",
            "aspects": {},
        },
        source_title="PU tubing kit",
        source_description="pu steel",
        qc_profile="motors",
        fact_sheet_conn=sqlite3.connect(":memory:"),
    )

    assert called["quality"] is False
    assert called["fact"] is False
    assert result["fact_sheet_status"] == "skipped"
    assert not any("semantic_material" in item for item in result["blockers"])


def test_motors_profile_blocks_incomplete_vehicle_fitment(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: _fact_sheet_result(),
    )

    result = listing_qc.run_listing_qc(
        sku="W3611P407492",
        candidate={
            "title": "3 inch Running Boards for Chevy trucks",
            "description": "Running boards for Chevy trucks and SUVs. Verify fitment before install.",
            "categoryId": "33650",
            "categoryName": "Running Boards & Step Bars",
            "aspects": {},
        },
        source_title="Running Boards Chevy Silverado",
        source_description="Chevy Silverado GMC Sierra",
        qc_profile="motors",
        fact_sheet_conn=sqlite3.connect(":memory:"),
    )

    assert result["status"] == "blocked"
    assert any("Fitment" in item for item in result["blockers"])


def test_motors_profile_passes_structured_fitment(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: _fact_sheet_result(),
    )

    result = listing_qc.run_listing_qc(
        sku="HITCH-OK",
        candidate={
            "title": "Class 3 Trailer Hitch 2 Inch Receiver for 2015-2020 Ford F-150",
            "description": "Fits 2015-2020 Ford F-150.",
            "categoryId": "33653",
            "categoryName": "Trailer Hitches",
            "aspects": {},
        },
        source_title="Trailer Hitch Ford F-150 2015-2020",
        source_description="2015 2016 2017 2018 2019 2020 Ford F-150",
        qc_profile="motors",
        fact_sheet_conn=None,
    )

    assert result["fact_sheet_status"] == "skipped"
    assert result["status"] == "pass"
    assert result["blockers"] == []


def _forbid_furniture_gate(monkeypatch):
    def boom_quality(*args, **kwargs):
        raise RuntimeError("furniture gate must not run")

    def boom_fact(**kwargs):
        raise RuntimeError("fact sheet must not run")

    monkeypatch.setattr(listing_qc, "validate_listing_quality", boom_quality)
    monkeypatch.setattr(listing_qc, "check_fact_sheet_violations", boom_fact)


def test_motors_universal_fit_blocks_pieces_vs_package_includes_1x(monkeypatch):
    """W2680P518198: universal-fit must still fail Set/Number of Pieces vs 1 x."""
    _forbid_furniture_gate(monkeypatch)
    aspects = {
        "Type": ["Floor Mat"],
        "Number of Pieces": ["4"],
        "Fitment Type": ["Universal Fit"],
    }
    candidate = {
        "title": "Universal Fit All Weather Floor Mat Set",
        "description": "<h3>PACKAGE INCLUDES</h3><p>1 x Floor Mat</p>",
        "categoryId": "179462",
        "categoryName": "Other Automotive Care Supplies",
        "aspects": aspects,
    }

    result = listing_qc.run_listing_qc(
        sku="W2680P518198",
        candidate=candidate,
        source_title=candidate["title"],
        source_description="Universal fit floor mat set, 4 pieces.",
        qc_profile="motors",
        fact_sheet_conn=None,
    )

    assert result["fact_sheet_status"] == "skipped"
    assert result["status"] == "blocked"
    assert any("package includes" in item.lower() for item in result["blockers"])
    assert not any(item.startswith("[Fitment]") for item in result["blockers"])
    assert candidate["aspects"] == aspects
    assert "compatibleProducts" not in candidate


def test_motors_universal_fit_blocks_set_word_vs_package_includes_1x(monkeypatch):
    _forbid_furniture_gate(monkeypatch)
    candidate = {
        "title": "Universal Fit Cabin Air Filter Set",
        "description": "<strong>PACKAGE INCLUDES</strong> 1 x Cabin Air Filter",
        "categoryId": "179462",
        "aspects": {"Type": ["Cabin Air Filter"], "Fitment Type": ["Universal Fit"]},
    }

    result = listing_qc.run_listing_qc(
        sku="W2680P518198",
        candidate=candidate,
        qc_profile="motors",
        fact_sheet_conn=None,
    )

    assert result["status"] == "blocked"
    assert result["quality_issues"][0]["code"] == "package_includes_set_qty_mismatch"
    assert not any(item.startswith("[Fitment]") for item in result["blockers"])


def test_motors_universal_fit_passes_when_package_qty_matches(monkeypatch):
    _forbid_furniture_gate(monkeypatch)
    candidate = {
        "title": "Universal Fit All Weather Floor Mat",
        "description": "<h3>PACKAGE INCLUDES</h3><p>4 x Floor Mat</p>",
        "categoryId": "179462",
        "aspects": {
            "Type": ["Floor Mat"],
            "Number of Pieces": ["4"],
            "Fitment Type": ["Universal Fit"],
        },
    }

    result = listing_qc.run_listing_qc(
        sku="MAT-OK",
        candidate=candidate,
        qc_profile="motors",
        fact_sheet_conn=None,
    )

    assert result["fact_sheet_status"] == "skipped"
    assert result["status"] == "pass"
    assert result["blockers"] == []


def test_motors_one_named_set_in_box_does_not_false_fail(monkeypatch):
    _forbid_furniture_gate(monkeypatch)
    candidate = {
        "title": "10Pcs Chrome Vanadium Socket Set",
        "description": "<h3>PACKAGE INCLUDES</h3><p>1 x Socket Set</p>",
        "categoryId": "43998",
        "aspects": {
            "Type": ["Socket Set"],
            "Number of Pieces": ["10"],
        },
    }

    result = listing_qc.run_listing_qc(
        sku="SOCKET-SET",
        candidate=candidate,
        qc_profile="motors",
        fact_sheet_conn=None,
    )

    assert result["status"] == "pass"
    assert result["blockers"] == []


def test_motors_fitment_block_still_reports_package_includes_mismatch(monkeypatch):
    _forbid_furniture_gate(monkeypatch)
    candidate = {
        "title": "Running Board Set for Chevy trucks",
        "description": (
            "Running boards for Chevy trucks and SUVs. Verify fitment before install."
            "<h3>PACKAGE INCLUDES</h3><p>1 x Running Board</p>"
        ),
        "categoryId": "33650",
        "aspects": {"Type": ["Running Board"], "Number of Pieces": ["2"]},
    }

    result = listing_qc.run_listing_qc(
        sku="W3611P407492",
        candidate=candidate,
        qc_profile="motors",
        fact_sheet_conn=None,
    )

    assert result["status"] == "blocked"
    assert any(item.startswith("[Fitment]") for item in result["blockers"])
    assert any("package includes" in item.lower() for item in result["blockers"])
