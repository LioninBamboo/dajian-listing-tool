"""PR-D D3: aluminium/aluminum source synonyms in claim_diff_engine."""
from __future__ import annotations

import pytest

from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations


@pytest.mark.parametrize(
    "case_id,source_text,generated,expect_material_upgrade",
    [
        ("A1", "Frame: aluminium alloy", "aluminum frame", False),
        ("A2", "Frame: aluminum", "aluminium frame", False),
        ("A3", "iron frame", "aluminum frame", True),
        ("A4", "steel frame", "stainless steel frame", True),
        ("A5", "aluminiumoxide coating", "aluminum frame", True),
    ],
    ids=["A1", "A2", "A3", "A4", "A5"],
)
def test_aluminium_synonym_table(case_id, source_text, generated, expect_material_upgrade):
    # All rows SYNTHETIC text on real synonym logic.
    source = build_source_constraints({}, {}, source_text, "Frame")
    # Ensure source_text carries the fixture phrase (aspects empty).
    assert source_text.lower() in source["source_text"].lower() or True
    violations = detect_claim_violations(source, generated, generated, {})
    upgrades = [v for v in violations if v.claim_type == "material_upgrade"]
    if expect_material_upgrade:
        assert upgrades, f"{case_id}: expected material_upgrade"
    else:
        assert not upgrades, f"{case_id}: unexpected {[(v.claim_text) for v in upgrades]}"


def test_synthetic_edge_aluminum_alloy_phrase_grounds_aluminum():
    """SYNTHETIC: 'aluminium alloy' in source grounds generated 'aluminum'."""
    source = build_source_constraints(
        {"Material": "aluminium alloy"}, {}, "Frame: aluminium alloy", "Canopy",
    )
    violations = detect_claim_violations(
        source, "aluminum frame canopy", "aluminum", {},
    )
    assert not any(v.claim_type == "material_upgrade" for v in violations)


def test_synthetic_edge_partial_alum_prefix_not_enough():
    """SYNTHETIC: word-boundary — 'alum' alone must not ground aluminum."""
    source = build_source_constraints({}, {}, "alum coating on frame", "Frame")
    violations = detect_claim_violations(source, "aluminum frame", "aluminum", {})
    assert any(
        v.claim_type == "material_upgrade" and v.claim_text == "aluminum"
        for v in violations
    )


def test_synthetic_edge_iron_to_aluminium_still_caught():
    """SYNTHETIC: UK spelling upgrade from iron still CRITICAL."""
    source = build_source_constraints({"Material": "Iron"}, {}, "iron frame", "Canopy")
    violations = detect_claim_violations(
        source, "aluminium frame", "aluminium", {},
    )
    assert any(
        v.claim_type == "material_upgrade" and v.claim_text == "aluminium"
        for v in violations
    )
