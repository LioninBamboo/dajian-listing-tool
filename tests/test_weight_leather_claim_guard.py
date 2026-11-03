"""Own-item weight is a supported fact; bare Leather ≠ PU Leather.

Spec table + boundary checklist for the shared invent gate used by listing QC
(motors/furniture), FactSheet pre-publish, READY-draft scrub, and batch_publish.
"""

from __future__ import annotations

import re

from src.services.listing_qc import (
    _LB_PER_KG,
    _span_supported,
    is_product_weight_label,
    merge_deterministic_fact_violations,
    motors_deterministic_fact_violations,
    structured_weight_mentions,
)
from src.utils.claim_diff_engine import (
    build_source_constraints,
    detect_claim_violations,
    leather_claim_kind,
    source_supports_leather_claim,
)
from src.utils.listing_fact_sheet import (
    _claim_in_text,
    _supported_by_any,
    compare_fact_sheets,
    lexical_materials,
)
from src.utils.ready_draft_claim_scrub import (
    collect_flagged_claims,
    scrub_candidate,
    whole_value_match,
)


def _cand(**overrides):
    payload = {
        "title": "Indoor Chair",
        "description": "<p>Indoor chair.</p>",
        "aspects": {"Brand": ["AquaVerve"], "Fitment Type": ["Universal"]},
        "categoryId": "33650",
    }
    payload.update(overrides)
    return payload


def _caps(candidate, **source):
    return motors_deterministic_fact_violations(
        candidate,
        source_title=source.get("title", "Indoor Chair"),
        source_description=source.get("description", "Indoor chair."),
        source_attributes=source.get("attributes") or {},
        source_specs=source.get("specs") or {},
    )


def _cap_texts(violations):
    return [
        str(item.get("claim_text") or "")
        for item in violations
        if str(item.get("claim_type") or "") == "semantic_capacity"
    ]


def _mat_texts(violations):
    return [
        str(item.get("claim_text") or "").lower()
        for item in violations
        if str(item.get("claim_type") or "") in {"semantic_material", "material_upgrade"}
    ]


def _joined(candidate):
    aspects = candidate.get("aspects") or {}
    aspect_text = " ".join(
        " ".join(str(v) for v in (val if isinstance(val, (list, tuple)) else [val]))
        for val in aspects.values()
    )
    return f"{candidate.get('title') or ''} {candidate.get('description') or ''} {aspect_text}"


# ── spec table ──────────────────────────────────────────────────────────────


def test_case1_item_weight_67_02_matches_source_field():
    cand = _cand(
        title="Chair",
        description="<p>Item Weight: 67.02 lbs</p>",
        aspects={"Item Weight": ["67.02 lbs"]},
    )
    violations = _caps(
        cand,
        attributes={"Product Weight (lbs.)": "67.02"},
    )
    assert not any("67.02" in text for text in _cap_texts(violations))
    flags = collect_flagged_claims(
        cand,
        {
            "title": "Chair",
            "description": "Chair.",
            "attributes": {"Product Weight (lbs.)": "67.02"},
            "specs": {},
        },
    )
    assert not any("67.02" in c.claim_text and c.claim_type == "semantic_capacity" for c in flags)


def test_case2_weighs_41_45_kept_by_scrub():
    cand = _cand(
        title="Patio chair weighs 41.45 lbs",
        description="<p>This unit weighs 41.45 lbs.</p>",
    )
    source = {
        "title": "Patio chair",
        "description": "Patio chair.",
        "attributes": {"Item Weight": "41.45"},
        "specs": {},
    }
    assert not any("41.45" in text for text in _cap_texts(_caps(cand, attributes={"Item Weight": "41.45"})))
    out, result = scrub_candidate(cand, source, sku="W2500P623899")
    assert "41.45" in _joined(out)
    assert "weighs 41.45" in (out.get("description") or "").lower()
    assert result.status == "unchanged"


def test_case3_weight_capacity_not_supported_by_item_weight():
    cand = _cand(
        title="Chair",
        description="<p>Weight Capacity: 250 lbs</p>",
    )
    texts = _cap_texts(_caps(cand, attributes={"Item Weight": "250"}))
    assert any("250" in text for text in texts)


def test_case4_weight_capacity_matches_labelled_load():
    cand = _cand(
        title="Chair",
        description="<p>Weight Capacity: 250 lbs</p>",
    )
    texts = _cap_texts(
        _caps(cand, description="Weight capacity 250 lbs.", attributes={"Item Weight": "80"})
    )
    assert not any("250" in text for text in texts)


def test_case5_mismatched_item_weight_flagged_not_corrected():
    cand = _cand(
        title="Chair",
        description="<p>Item Weight: 70 lbs</p>",
        aspects={"Item Weight": ["70 lbs"]},
    )
    source = {
        "title": "Chair",
        "description": "Chair.",
        "attributes": {"Item Weight": "67.02"},
        "specs": {},
    }
    texts = _cap_texts(_caps(cand, attributes={"Item Weight": "67.02"}))
    assert any("70" in text for text in texts)
    out, result = scrub_candidate(cand, source, sku="WEIGHT-MISMATCH")
    assert "70" in _joined(out)
    assert "67.02" not in _joined(out)
    assert not any(diff.action == "source_correct" for diff in result.diffs)


def test_case6_unlabelled_net_and_package_weights_pass():
    cand = _cand(
        title="Cabinet",
        description="<p>77 lbs. 84 lbs.</p>",
    )
    texts = _cap_texts(
        _caps(
            cand,
            attributes={"Item Weight": "77", "Package Weight (lbs.)": "84"},
        )
    )
    assert not any("77" in text or "84" in text for text in texts)


def test_case7_supports_up_to_item_weight_is_capacity():
    cand = _cand(
        title="Chair supports up to 33.1 lbs",
        description="<p>Supports up to 33.1 lbs.</p>",
    )
    texts = _cap_texts(_caps(cand, attributes={"Item Weight": "33.1"}))
    assert any("33.1" in text for text in texts)


def test_case8_kg_item_weight_uses_existing_conversion_tolerance():
    cand = _cand(
        title="Chair",
        description="<p>Item Weight: 30.5 kg</p>",
        aspects={"Item Weight": ["30.5 kg"]},
    )
    texts = _cap_texts(_caps(cand, attributes={"Item Weight": "67.24 lb"}))
    claim = (30.5 * _LB_PER_KG, 30.5 * _LB_PER_KG)
    source = (67.24, 67.24)
    assert _span_supported(claim, source, convert=True) is True
    assert not any("30.5" in text for text in texts)


def test_case9_bare_leather_vs_pu_flagged_and_replaced():
    cand = _cand(
        title="Leather sofa",
        description="<p>Leather sofa.</p>",
        aspects={"Material": ["Leather"]},
    )
    source = {
        "title": "Sofa",
        "description": "PU Leather sofa.",
        "attributes": {"Material": "PU Leather"},
        "specs": {},
    }
    mats = _mat_texts(_caps(cand, description="PU Leather sofa.", attributes={"Material": "PU Leather"}))
    assert any(text == "leather" for text in mats)
    out, result = scrub_candidate(cand, source, sku="W527P510517")
    text = _joined(out)
    assert "PU Leather" in text
    assert out["title"] == "PU Leather sofa"
    assert out["aspects"]["Material"] in (["PU Leather"], "PU Leather")
    assert result.status == "cleaned"
    assert any(diff.action == "source_correct" for diff in result.diffs)


def test_case10_pu_leather_with_pu_source_passes():
    cand = _cand(
        title="PU Leather sofa",
        description="<p>PU Leather sofa.</p>",
        aspects={"Material": ["PU Leather"]},
    )
    mats = _mat_texts(_caps(cand, description="PU Leather sofa.", attributes={"Material": "PU Leather"}))
    assert not any(text == "leather" for text in mats)
    assert not any("genuine" in text for text in mats)


def test_case11_faux_and_pu_are_both_synthetic():
    """Choice: Faux Leather vs PU Leather PASSES (both synthetic, never 'genuine')."""
    cand = _cand(
        title="Faux Leather sofa",
        description="<p>Faux Leather sofa.</p>",
        aspects={"Material": ["Faux Leather"]},
    )
    mats = _mat_texts(_caps(cand, description="PU Leather sofa.", attributes={"Material": "PU Leather"}))
    assert not any("genuine" in text for text in mats)
    assert not any(text == "leather" for text in mats)


def test_case12_genuine_leather_vs_multi_material_is_removed():
    cand = _cand(
        title="Genuine Leather sofa",
        description="<p>Genuine Leather sofa.</p>",
        aspects={"Material": ["Genuine Leather"]},
    )
    source = {
        "title": "Sofa",
        "description": "Frame and upholstery.",
        "attributes": {"Material": "PU Leather, Wood"},
        "specs": {},
    }
    mats = _mat_texts(_caps(cand, attributes={"Material": "PU Leather, Wood"}))
    assert any("genuine leather" in text or text == "leather" for text in mats)
    out, result = scrub_candidate(cand, source, sku="GEN-MULTI")
    text = _joined(out).lower()
    assert "genuine leather" not in text
    assert "pu leather, wood" not in text
    assert result.status == "cleaned"
    assert all(diff.action == "remove" for diff in result.diffs)


def test_case13_leather_with_leather_source_passes():
    cand = _cand(
        title="Leather sofa",
        description="<p>Leather sofa.</p>",
        aspects={"Material": ["Leather"]},
    )
    mats = _mat_texts(_caps(cand, description="Leather sofa.", attributes={"Material": "Leather"}))
    assert not any(text == "leather" for text in mats)


def test_case14_leatherette_does_not_hit_bare_leather():
    cand = _cand(
        title="Leatherette bench",
        description="<p>Leatherette bench.</p>",
        aspects={"Material": ["Leatherette"]},
    )
    mats = _mat_texts(_caps(cand, description="PU finish.", attributes={"Material": "PU"}))
    assert not any(text == "leather" for text in mats)
    assert leather_claim_kind("leatherette") == "synthetic"
    found = lexical_materials("leatherette bench")
    assert "leatherette" in found
    assert "leather" not in found
    assert whole_value_match("Leather", "Leatherette bench") is False


def test_case15_missing_source_weight_stays_flagged():
    cand = _cand(
        title="Chair",
        description="<p>Item Weight: 20 lbs</p>",
    )
    for attrs in ({}, {"Item Weight": None}, {"Item Weight": "n/a"}, {"Item Weight": "abc"}):
        texts = _cap_texts(_caps(cand, attributes=attrs))
        assert any("20" in text for text in texts), attrs


# ── same rule on furniture QC / claim_diff / factsheet ──────────────────────


def test_furniture_qc_accepts_sourced_item_weight(monkeypatch):
    from src.services import listing_qc as qc_mod

    monkeypatch.setattr(qc_mod, "validate_listing_quality", lambda *a, **k: [])
    monkeypatch.setattr(
        qc_mod,
        "check_fact_sheet_violations",
        lambda **kwargs: {
            "status": "pass",
            "violations": [],
            "source_fingerprint": "s",
            "candidate_fingerprint": "c",
        },
    )
    result = qc_mod.run_listing_qc(
        sku="W3224P556674",
        candidate=_cand(
            title="Chair",
            description="<p>Item Weight: 67.02 lbs</p>",
            aspects={"Item Weight": ["67.02 lbs"]},
        ),
        source_title="Chair",
        source_description="Chair.",
        source_attributes={"Product Weight (lbs.)": "67.02"},
        fact_sheet_conn=object(),
        qc_profile="furniture",
    )
    assert not any("semantic_capacity" in item and "67.02" in item for item in result["blockers"])


def test_furniture_qc_flags_bare_leather_against_pu(monkeypatch):
    from src.services import listing_qc as qc_mod

    monkeypatch.setattr(qc_mod, "validate_listing_quality", lambda *a, **k: [])
    monkeypatch.setattr(
        qc_mod,
        "check_fact_sheet_violations",
        lambda **kwargs: {
            "status": "pass",
            "violations": [],
            "source_fingerprint": "s",
            "candidate_fingerprint": "c",
        },
    )
    result = qc_mod.run_listing_qc(
        sku="W527P510517",
        candidate=_cand(
            title="Leather sofa",
            description="<p>Leather sofa.</p>",
            aspects={"Material": ["Leather"]},
        ),
        source_title="Sofa",
        source_description="PU Leather sofa.",
        source_attributes={"Material": "PU Leather"},
        fact_sheet_conn=object(),
        qc_profile="furniture",
    )
    assert result["status"] == "blocked"
    assert any("leather" in item.lower() for item in result["blockers"])


def test_claim_diff_bare_leather_vs_pu():
    source = build_source_constraints({"Material": "PU Leather"}, {}, "Nice sofa", "Sofa")
    flagged = detect_claim_violations(source, "Leather sofa", "Leather sofa.", {"Material": "Leather"})
    assert any(v.claim_text == "leather" for v in flagged)
    ok = detect_claim_violations(
        source, "PU Leather sofa", "PU Leather sofa.", {"Material": "PU Leather"}
    )
    assert not any(v.claim_text == "leather" for v in ok)
    genuine_src = build_source_constraints({"Material": "Genuine Leather"}, {}, "Sofa", "Sofa")
    genuine = detect_claim_violations(
        genuine_src, "Leather sofa", "Leather sofa.", {"Material": "Leather"}
    )
    assert not any(v.claim_text == "leather" for v in genuine)


def test_factsheet_compare_leather_whole_phrase():
    source = {
        "materials": ["pu leather"],
        "features": [],
        "counts": {},
        "capacity": None,
        "certifications": [],
        "dimensions": {},
    }
    live_leather = dict(source, materials=["leather"])
    hits = compare_fact_sheets(
        source,
        live_leather,
        live_text="Leather sofa",
        source_text="Material: PU Leather",
    )
    assert any(v["claim_type"] == "semantic_material" and v["claim_text"] == "leather" for v in hits)
    live_pu = dict(source, materials=["pu leather"])
    assert not any(
        v["claim_type"] == "semantic_material"
        for v in compare_fact_sheets(
            source, live_pu, live_text="PU Leather sofa", source_text="Material: PU Leather"
        )
    )


# ── boundary checklist ──────────────────────────────────────────────────────


def test_whole_label_weight_capacity_is_not_weight_fact():
    assert is_product_weight_label("Weight") is True
    assert is_product_weight_label("Item Weight") is True
    assert is_product_weight_label("Product Weight (lbs.)") is True
    assert is_product_weight_label("Package Weight (lbs.)") is True
    assert is_product_weight_label("Weight Capacity") is False
    assert is_product_weight_label("Maximum Weight Capacity") is False
    assert is_product_weight_label("Load Capacity") is False
    assert whole_value_match("Leather", "PU Leather sofa") is False
    assert whole_value_match("Leather", "Leatherette") is False
    assert leather_claim_kind("PU Leather") == "synthetic"
    assert leather_claim_kind("Leather") == "bare"


def test_normalization_lbs_lb_pounds_and_fullwidth():
    mentions = structured_weight_mentions({"Ｉｔｅｍ　Ｗｅｉｇｈｔ": "６７.０２"})
    assert mentions and mentions[0][0] == "67.02"
    mentions_ascii = structured_weight_mentions({"Item Weight": "67.02 pounds"})
    assert mentions_ascii
    assert mentions_ascii[0][0] == "67.02"
    lb = structured_weight_mentions({"Item Weight": "67.02 lb"})
    lbs = structured_weight_mentions({"Item Weight": "67.02 lbs"})
    assert lb[0][0] == lbs[0][0] == "67.02"


def test_empty_none_missing_weight_no_crash_no_silent_pass():
    assert structured_weight_mentions({"Item Weight": None}) == []
    assert structured_weight_mentions({"Item Weight": ""}) == []
    assert structured_weight_mentions({}) == []
    assert source_supports_leather_claim("leather") is False
    assert source_supports_leather_claim("leather", "") is False
    assert _claim_in_text("leather", "") is False


def test_multiple_source_weights_any_match_wins():
    mentions = structured_weight_mentions(
        {"Item Weight": "77", "Package Weight (lbs.)": "84"},
    )
    numbers = {number for number, _family, _raw in mentions}
    assert numbers == {"77", "84"}
    cand = _cand(description="<p>Item Weight: 77 lbs. Package Weight: 84 lbs.</p>")
    assert not _cap_texts(
        _caps(cand, attributes={"Item Weight": "77", "Package Weight (lbs.)": "84"})
    )


def test_unknown_weight_not_mapped_to_nearest_guess():
    cand = _cand(description="<p>Item Weight: 70 lbs</p>")
    texts = _cap_texts(_caps(cand, attributes={"Item Weight": "67.02"}))
    assert any("70" in text for text in texts)
    assert not any("67.02" in text and "70" not in text for text in texts)


def test_numbers_zero_negative_decimals_ranges_unit_missing():
    assert structured_weight_mentions({"Item Weight": "0"}) == []
    assert structured_weight_mentions({"Item Weight": "-5"}) == []
    assert structured_weight_mentions({"Item Weight": "67.02"})[0][0] == "67.02"
    unitless = _caps(_cand(description="<p>Item Weight: 20</p>"), attributes={"Item Weight": "50"})
    unitless_texts = _cap_texts(unitless)
    assert not any(re.search(r"\b20\b", text) for text in unitless_texts)
    assert not any(re.search(r"\b20\s*(?:lbs?|pounds?)\b", text, re.I) for text in unitless_texts)
    ranged = _caps(
        _cand(title="6 person tent", description="<p>6 person tent.</p>"),
        description="Occupancy 4-6 person.",
        title="4-6 person tent",
    )
    assert any("6" in text for text in _cap_texts(ranged))


def test_conversion_tolerance_constant():
    # Existing load/weight compare: max(0.6 lb, 2% of source magnitude) in pounds.
    close = _span_supported((67.0, 67.0), (67.02, 67.02), convert=True)
    far = _span_supported((70.0, 70.0), (67.02, 67.02), convert=True)
    assert close is True
    assert far is False


def test_supported_by_any_does_not_count_leather_inside_pu():
    assert _supported_by_any("leather", ["pu leather"], set(), "pu leather") is False
    assert _supported_by_any("pu leather", ["pu leather"], set(), "pu leather") is True
    assert _claim_in_text("leather", "PU Leather sofa") is False
    assert _claim_in_text("pu leather", "PU Leather sofa") is True


# ── follow-up: capacity phrasing + standalone PU ─────────────────────────────


def _claim_vs_item_weight(claim: str):
    cand = _cand(title="Chair", description=f"<p>{claim}</p>")
    return _cap_texts(_caps(cand, attributes={"Item Weight": "45"}))


def _claim_vs_labelled_load(claim: str):
    cand = _cand(title="Chair", description=f"<p>{claim}</p>")
    return _cap_texts(
        _caps(
            cand,
            description="Weight capacity 45 lbs.",
            attributes={"Item Weight": "20"},
        )
    )


def _assert_flagged_45(texts, claim: str) -> None:
    assert any("45" in text for text in texts), f"{claim!r} should flag vs item weight 45; got {texts}"


def _assert_pass_45(texts, claim: str) -> None:
    assert not any("45" in text for text in texts), f"{claim!r} should pass vs labelled load 45; got {texts}"


def test_phrasing_45_lb_capacity_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("45 lb capacity"), "45 lb capacity")


def test_phrasing_45_lb_capacity_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("45 lb capacity"), "45 lb capacity")


def test_phrasing_maximum_weight_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("Maximum Weight 45 lbs"), "Maximum Weight 45 lbs")


def test_phrasing_maximum_weight_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("Maximum Weight 45 lbs"), "Maximum Weight 45 lbs")


def test_phrasing_max_dot_load_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("Max. Load: 45 lbs"), "Max. Load: 45 lbs")


def test_phrasing_max_dot_load_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("Max. Load: 45 lbs"), "Max. Load: 45 lbs")


def test_phrasing_capacity_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("Capacity 45 lbs"), "Capacity 45 lbs")


def test_phrasing_capacity_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("Capacity 45 lbs"), "Capacity 45 lbs")


def test_phrasing_bears_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("Bears 45 lbs"), "Bears 45 lbs")


def test_phrasing_bears_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("Bears 45 lbs"), "Bears 45 lbs")


def test_phrasing_load_capacity_of_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("load capacity of 45 lbs"), "load capacity of 45 lbs")


def test_phrasing_load_capacity_of_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("load capacity of 45 lbs"), "load capacity of 45 lbs")


def test_phrasing_holds_up_to_45_lb_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("holds up to 45 lb"), "holds up to 45 lb")


def test_phrasing_holds_up_to_45_lb_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("holds up to 45 lb"), "holds up to 45 lb")


def test_phrasing_rated_for_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("rated for 45 lbs"), "rated for 45 lbs")


def test_phrasing_rated_for_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("rated for 45 lbs"), "rated for 45 lbs")


def test_phrasing_weight_limit_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("weight limit 45 lbs"), "weight limit 45 lbs")


def test_phrasing_weight_limit_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("weight limit 45 lbs"), "weight limit 45 lbs")


def test_phrasing_max_weight_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("max weight 45 lbs"), "max weight 45 lbs")


def test_phrasing_max_weight_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("max weight 45 lbs"), "max weight 45 lbs")


def test_phrasing_maximum_load_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("maximum load 45 lbs"), "maximum load 45 lbs")


def test_phrasing_maximum_load_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("maximum load 45 lbs"), "maximum load 45 lbs")


def test_phrasing_loading_capacity_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("loading capacity 45 lbs"), "loading capacity 45 lbs")


def test_phrasing_loading_capacity_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("loading capacity 45 lbs"), "loading capacity 45 lbs")


def test_phrasing_supports_45_lbs_vs_item_weight_flagged():
    _assert_flagged_45(_claim_vs_item_weight("supports 45 lbs"), "supports 45 lbs")


def test_phrasing_supports_45_lbs_vs_labelled_load_passes():
    _assert_pass_45(_claim_vs_labelled_load("supports 45 lbs"), "supports 45 lbs")


def test_item_weight_45_lbs_vs_source_weight_45_passes():
    texts = _cap_texts(
        _caps(
            _cand(title="Chair", description="<p>Item Weight: 45 lbs</p>"),
            attributes={"Item Weight": "45"},
        )
    )
    assert not any("45" in text for text in texts)


def test_maximum_weight_45_lbs_vs_source_weight_45_flagged():
    texts = _cap_texts(
        _caps(
            _cand(title="Chair", description="<p>Maximum Weight 45 lbs</p>"),
            attributes={"Item Weight": "45"},
        )
    )
    assert any("45" in text for text in texts)


def test_merge_keeps_llm_capacity_flag_when_det_misses_own_weight():
    cand = _cand(title="Chair", description="<p>45 lb capacity</p>")
    llm = [
        {
            "claim_type": "semantic_capacity",
            "claim_text": "45 lb capacity",
            "severity": "HIGH",
            "source_evidence": "NOT_FOUND",
        }
    ]
    merged = merge_deterministic_fact_violations(
        llm,
        cand,
        source_title="Chair",
        source_description="Chair.",
        source_attributes={"Item Weight": "45"},
        source_specs={},
    )
    texts = _cap_texts(merged)
    assert any("45" in text for text in texts)
    assert any("capacity" in text.lower() for text in texts)


def test_pu_leather_draft_vs_pu_foam_cushions_flagged():
    source_desc = "Fabric sofa with high-density PU foam cushions"
    assert source_supports_leather_claim("PU Leather", source_desc) is False
    cand = _cand(
        title="PU Leather sofa",
        description="<p>PU Leather sofa.</p>",
        aspects={"Material": ["PU Leather"]},
    )
    mats = _mat_texts(_caps(cand, description=source_desc))
    assert any("pu leather" in text for text in mats)


def test_pu_leather_draft_vs_material_pu_passes():
    assert source_supports_leather_claim("PU Leather", "PU") is True
    cand = _cand(
        title="PU Leather sofa",
        description="<p>PU Leather sofa.</p>",
        aspects={"Material": ["PU Leather"]},
    )
    mats = _mat_texts(_caps(cand, attributes={"Material": "PU"}))
    assert not any("pu leather" in text for text in mats)


def test_pu_leather_draft_vs_pu_wheels_flagged():
    assert source_supports_leather_claim("PU Leather", "PU wheels") is False
    cand = _cand(
        title="PU Leather sofa",
        description="<p>PU Leather sofa.</p>",
        aspects={"Material": ["PU Leather"]},
    )
    mats = _mat_texts(_caps(cand, description="Chair with PU wheels."))
    assert any("pu leather" in text for text in mats)


def test_pu_leather_draft_vs_pu_coating_flagged():
    assert source_supports_leather_claim("PU Leather", "PU coating") is False
    cand = _cand(
        title="PU Leather sofa",
        description="<p>PU Leather sofa.</p>",
        aspects={"Material": ["PU Leather"]},
    )
    mats = _mat_texts(_caps(cand, description="Steel frame with PU coating."))
    assert any("pu leather" in text for text in mats)


def test_pu_leather_draft_vs_faux_leather_text_passes():
    assert source_supports_leather_claim("PU Leather", "Faux Leather sofa") is True
    cand = _cand(
        title="PU Leather sofa",
        description="<p>PU Leather sofa.</p>",
        aspects={"Material": ["PU Leather"]},
    )
    mats = _mat_texts(_caps(cand, description="Faux Leather sofa."))
    assert not any("pu leather" in text for text in mats)
