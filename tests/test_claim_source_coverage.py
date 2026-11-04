"""GIGA paraphrase coverage: owner table, checklist, extra edges.

Same rule is used by FactSheet compare, listing QC / batch_publish,
audit_fix_ready_drafts, and READY-draft claim scrub.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from src.services import listing_qc
from src.utils.claim_source_coverage import (
    CLAIM_PARAPHRASE_SYNONYMS,
    FEATURE_CLAIM_TYPES,
    SOURCE_FIELD_PRECEDENCE,
    _extract_counts,
    content_tokens,
    filter_supported_paraphrase_violations,
    is_bare_leather_claim,
    is_hard_claim_phrase,
    join_source_fields,
    normalize_for_coverage,
    phrase_in_text,
    source_supports_claim_phrase,
)
from src.utils.ready_draft_claim_scrub import FlaggedClaim
from src.utils.listing_fact_sheet import compare_fact_sheets
from src.utils.ready_draft_claim_scrub import collect_flagged_claims, scrub_candidate


ROOT = Path(__file__).resolve().parents[1]

OWNER_SUPPORTED = [
    ("W2105P595057", "rubberized non-slip foot pads", "anti-slip rubber foot pads"),
    ("W2105P595057", "high-density rebound cushioning", "high-density rebound filling"),
    ("W2105P595057", "padded seat", "seat cushion with high-density resilient foam"),
    ("W2105P595057", "barrelback", "Curved Barrel back"),
    ("W2105P595057", "compact footprint", "compact size"),
    ("W5870P523908", "one-seat", "Seats: 1 Seat"),
    ("W5870P523908", "shaped", "in Pumpkin Shape"),
    ("W5568P534216", "upholstered", "Floral Upholstery"),
    ("W2500P623899", "cushioned", "full-surround all-around cushion"),
    ("W331P511605", "open shelving", "Four open sections for shoes. Open storage."),
]

OWNER_FLAG = [
    ("W5568P534216", "full armrests", "Accent Chair"),
    ("W5568P534216", "Armchair", "Accent Chair"),
    ("W331P511605", "warp-resistant", ""),
    ("W331P511605", "assemble in 30 mins", ""),
    ("W331P511605", "includes all hardware", ""),
    ("W5870P523908", "No Assembly Required", ""),
    ("W5870P523908", "ultra-plush", "Rabbit Fur"),
    ("any", "solid wood", "MDF / particle board"),
    ("any", "Leather", "PU Leather"),
    ("any", "Load capacity 45 lbs", ""),
    ("any", "multi-room versatility", ""),
]


def _sheet(**overrides):
    payload = {
        "materials": [],
        "features": [],
        "counts": {},
        "capacity": None,
        "certifications": [],
        "dimensions": {},
    }
    payload.update(overrides)
    return payload


def _cand(**overrides):
    payload = {
        "title": "Indoor Chair",
        "description": "<p>Indoor chair.</p>",
        "aspects": {"Brand": ["AquaVerve"]},
    }
    payload.update(overrides)
    return payload


def _src(**overrides):
    payload = {
        "title": "Indoor Chair",
        "description": "Indoor chair.",
        "attributes": {},
        "specs": {},
    }
    payload.update(overrides)
    return payload


def _feature_violations(claim: str, source_text: str, live_text: str | None = None):
    return [
        item
        for item in compare_fact_sheets(
            _sheet(),
            _sheet(features=[claim]),
            source_text=source_text,
            live_text=live_text or claim,
        )
        if item["claim_type"] == "semantic_feature"
    ]


@pytest.mark.parametrize("sku,claim,source", OWNER_SUPPORTED)
def test_owner_table_supported(sku, claim, source):
    assert source_supports_claim_phrase(claim, source) is True, (sku, claim, source)
    assert _feature_violations(claim, source) == []


@pytest.mark.parametrize("sku,claim,source", OWNER_FLAG)
def test_owner_table_flagged(sku, claim, source):
    assert source_supports_claim_phrase(claim, source) is False, (sku, claim, source)


def test_owner_table_hard_claims_still_flag_in_factsheet():
    solid = compare_fact_sheets(
        _sheet(materials=["mdf", "particle board"]),
        _sheet(materials=["solid wood"]),
        source_text="MDF / particle board",
        live_text="solid wood desk",
    )
    assert any(item["claim_type"] == "semantic_material" and "solid wood" in item["claim_text"] for item in solid)

    leather = compare_fact_sheets(
        _sheet(materials=["pu leather"]),
        _sheet(materials=["leather"]),
        source_text="PU Leather sofa",
        live_text="Leather sofa",
    )
    assert any(item["claim_type"] == "semantic_material" and item["claim_text"] == "leather" for item in leather)

    load = [
        item
        for item in compare_fact_sheets(
            _sheet(),
            _sheet(features=["load capacity 45 lbs"]),
            source_text="Accent chair.",
            live_text="Load capacity 45 lbs",
        )
        if "45" in item.get("claim_text", "")
    ]
    assert load

    assert _feature_violations("full armrests", "Accent Chair")
    assert _feature_violations("warp-resistant", "")
    assert _feature_violations("assemble in 30 mins", "")
    assert _feature_violations("includes all hardware", "")
    assert _feature_violations("No Assembly Required", "")
    assert _feature_violations("ultra-plush", "Rabbit Fur")
    assert _feature_violations("multi-room versatility", "")


def test_whole_value_not_substring():
    assert phrase_in_text("table", "stable platform") is False
    assert phrase_in_text("table", "oak table") is True
    assert phrase_in_text("Leather", "PU Leather sofa") is True  # word leather is present
    assert is_bare_leather_claim("Leather") is True
    assert source_supports_claim_phrase("Leather", "PU Leather sofa") is False
    assert source_supports_claim_phrase("PU Leather", "Leather sofa") is False
    assert source_supports_claim_phrase("stable", "table") is False
    assert source_supports_claim_phrase("table", "stable") is False


def test_normalization_once_same_on_both_sides():
    left = "Black ＆ White 　Chairs"
    right = "black and white chairs"
    assert normalize_for_coverage(left) == normalize_for_coverage(right)
    assert normalize_for_coverage("a\r\nb") == "a b"
    assert normalize_for_coverage("２　Pack") == "2 pack"
    assert source_supports_claim_phrase("padded seat", "SEAT  CUSHION") is True
    assert source_supports_claim_phrase("one-seat", "Seats:\r\n1 Seat") is True


def test_empty_none_missing_source_never_supported_or_crash():
    assert source_supports_claim_phrase("padded seat", None) is False
    assert source_supports_claim_phrase("padded seat", "") is False
    assert source_supports_claim_phrase("padded seat", {}, []) is False
    assert source_supports_claim_phrase(None, "seat cushion") is False
    assert source_supports_claim_phrase("", "seat cushion") is False
    assert join_source_fields(None, "", {}, []) == ""
    flags = collect_flagged_claims({}, None)
    assert flags == []
    assert _feature_violations("padded seat", "")


def test_any_source_field_may_support_and_precedence_is_stated():
    assert "any field" in SOURCE_FIELD_PRECEDENCE
    assert source_supports_claim_phrase("compact footprint", "compact size", "", {}, {}) is True
    assert source_supports_claim_phrase(
        "one-seat",
        "",
        "",
        {"Seats": "1 Seat"},
        {},
    ) is True
    assert source_supports_claim_phrase(
        "open shelving",
        "",
        "",
        {},
        {"Storage": "Four open sections"},
    ) is True
    assert source_supports_claim_phrase("padded seat", "", "no mention of comfort", {"Color": "Black"}) is False


def test_unknown_words_are_not_fuzzy_matched():
    assert source_supports_claim_phrase("cushon", "cushion") is False
    assert source_supports_claim_phrase("blorble pads", "anti-slip rubber foot pads") is False
    assert source_supports_claim_phrase("rubberised", "rubber") is False


def test_numeric_claims_stay_exact():
    assert is_hard_claim_phrase("0 lb") is True
    assert is_hard_claim_phrase("45 lbs") is True
    assert is_hard_claim_phrase("Load capacity 45 lbs") is True
    assert is_hard_claim_phrase("assemble in 30 mins") is True
    assert is_hard_claim_phrase("12.5 lb") is True
    assert source_supports_claim_phrase("0 lb", "0 lb hook") is False
    assert source_supports_claim_phrase("12.5 lb", "12 lb") is False
    assert source_supports_claim_phrase("4-6", "6") is False
    assert source_supports_claim_phrase("1/2", "1") is False
    assert source_supports_claim_phrase("45 lbs", "45 lbs") is False
    assert source_supports_claim_phrase("Load capacity 45 lbs", "Load capacity 45 lbs") is False
    assert source_supports_claim_phrase("4-6 person", "6 person") is False


def test_marketing_words_alone_never_support():
    assert content_tokens("smart design") == []
    assert source_supports_claim_phrase("smart", "smart storage") is False
    assert source_supports_claim_phrase("design", "modern design chair") is False
    assert source_supports_claim_phrase("versatile", "versatile chair") is False
    assert source_supports_claim_phrase("organization", "home organization") is False
    assert source_supports_claim_phrase("multi-room versatility", "living room") is False


def test_synonym_table_is_one_small_structure():
    assert isinstance(CLAIM_PARAPHRASE_SYNONYMS, tuple)
    assert 1 <= len(CLAIM_PARAPHRASE_SYNONYMS) <= 12
    joined = " ".join(sorted({phrase for group in CLAIM_PARAPHRASE_SYNONYMS for phrase in group}))
    assert "anti-slip" in joined and "non-slip" in joined
    assert "rubberized" in joined
    assert "resilient" in joined and "rebound" in joined
    assert "padded" in joined and "cushion" in joined
    assert "upholstery" in joined
    assert "open shelving" in joined
    assert "compact footprint" in joined
    assert "space-saving" in joined
    assert "countertop" in joined


def test_extra_edges_hyphen_plural_countertop_and_space_saving():
    assert source_supports_claim_phrase("barrel-back", "Curved Barrel back") is True
    assert source_supports_claim_phrase("foot pads", "anti-slip rubber foot pad") is True
    assert source_supports_claim_phrase("countertop friendly", "Bamboo Countertop Rack") is True
    assert source_supports_claim_phrase("space-saving", "won't take up extra space") is True
    assert source_supports_claim_phrase("space-smart", "space-saving cube") is True
    assert source_supports_claim_phrase("resilient", "high-density rebound filling") is True


def test_factsheet_medium_paraphrase_does_not_flag():
    violations = compare_fact_sheets(
        _sheet(features=["anti-slip rubber foot pads"]),
        _sheet(features=["rubberized non-slip foot pads"]),
        source_text="anti-slip rubber foot pads",
        live_text="rubberized non-slip foot pads",
    )
    assert violations == []


def test_listing_qc_drops_supported_medium_feature(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: {
            "status": "violations",
            "violations": [
                {
                    "claim_type": "semantic_feature",
                    "claim_text": "rubberized non-slip foot pads",
                    "severity": "MEDIUM",
                    "source_evidence": "NOT_FOUND",
                }
            ],
            "source_fingerprint": "s",
            "candidate_fingerprint": "c",
        },
    )
    monkeypatch.setattr(listing_qc, "merge_deterministic_fact_violations", lambda violations, *a, **k: list(violations))
    result = listing_qc.run_listing_qc(
        sku="W2105P595057",
        candidate={
            "title": "Chair with rubberized non-slip foot pads",
            "description": "<div><h2>KEY FEATURES</h2><ul><li>rubberized non-slip foot pads</li></ul></div>",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Barrel Chair",
        source_description="anti-slip rubber foot pads",
        source_attributes={},
        source_specs={},
        images=["a", "b"],
        fact_sheet_conn=object(),
        qc_profile="furniture",
    )
    assert result["fact_sheet_violations"] == []
    assert result["fact_sheet_status"] == "pass"
    assert result["status"] == "pass"
    assert result["blockers"] == []


def test_listing_qc_keeps_hard_medium_or_high_feature(monkeypatch):
    monkeypatch.setattr(listing_qc, "validate_listing_quality", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        listing_qc,
        "check_fact_sheet_violations",
        lambda **kwargs: {
            "status": "violations",
            "violations": [
                {
                    "claim_type": "semantic_feature",
                    "claim_text": "warp-resistant",
                    "severity": "HIGH",
                    "source_evidence": "NOT_FOUND",
                }
            ],
            "source_fingerprint": "s",
            "candidate_fingerprint": "c",
        },
    )
    monkeypatch.setattr(listing_qc, "merge_deterministic_fact_violations", lambda violations, *a, **k: list(violations))
    result = listing_qc.run_listing_qc(
        sku="W331P511605",
        candidate={
            "title": "warp-resistant shelf",
            "description": "<div><h2>KEY FEATURES</h2><ul><li>warp-resistant</li></ul></div>",
            "aspects": {"Brand": ["AquaVerve"]},
        },
        source_title="Shelf",
        source_description="Four open sections. Open storage.",
        source_attributes={},
        source_specs={},
        images=["a", "b"],
        fact_sheet_conn=object(),
        qc_profile="furniture",
    )
    assert result["fact_sheet_violations"]
    assert result["fact_sheet_violations"][0]["claim_text"] == "warp-resistant"
    assert result["status"] == "blocked"


def test_claim_scrub_keeps_supported_paraphrase():
    cand = _cand(
        title="Cushioned barrelback chair",
        description="<p>rubberized non-slip foot pads. padded seat. compact footprint.</p>",
    )
    source = _src(
        title="Curved Barrel Chair compact size",
        description="anti-slip rubber foot pads. seat cushion with high-density resilient foam.",
    )
    before = collect_flagged_claims(cand, source)
    paraphrase_hits = [
        item
        for item in before
        if any(
            token in item.claim_text.lower()
            for token in ("rubberized", "padded", "barrel", "compact", "cushion")
        )
    ]
    assert paraphrase_hits == []
    out, result = scrub_candidate(cand, source, sku="W2105P595057")
    text = f"{out['title']} {out['description']}".lower()
    assert "rubberized" in text
    assert "padded" in text
    assert "barrelback" in text
    assert result.status == "unchanged"


def test_claim_scrub_still_flags_hard_absences():
    cand = _cand(
        title="Solid wood Armchair 45 lb",
        description="<p>Load capacity 45 lbs. No Assembly Required. Leather. warp-resistant.</p>",
        aspects={"Material": ["Solid Wood", "Leather"]},
    )
    source = _src(
        title="Accent Chair",
        description="MDF / particle board. PU Leather.",
        attributes={"Material": "MDF"},
    )
    flags = collect_flagged_claims(cand, source)
    texts = [item.claim_text.lower() for item in flags]
    types = [item.claim_type.lower() for item in flags]
    assert any("45" in text for text in texts)
    assert any("solid wood" in text for text in texts)
    assert any(text == "leather" or "leather" in text for text in texts)
    assert any(item == "semantic_capacity" for item in types)
    assert any(item in {"material_upgrade", "semantic_material", "claim_material_upgrade"} for item in types)
    out, result = scrub_candidate(cand, source, sku="HARD")
    text = f"{out['title']} {out['description']}".lower()
    assert result.status == "cleaned"
    assert "45" not in text
    assert "solid wood" not in text
    assert "leather" not in text or "pu leather" in text


def test_filter_does_not_drop_material_or_capacity():
    kept = filter_supported_paraphrase_violations(
        [
            {"claim_type": "semantic_material", "claim_text": "solid wood", "severity": "CRITICAL"},
            {"claim_type": "semantic_capacity", "claim_text": "45 lbs", "severity": "HIGH"},
            {"claim_type": "semantic_feature", "claim_text": "padded seat", "severity": "MEDIUM"},
        ],
        "seat cushion. MDF. no load figure.",
    )
    types = {item["claim_type"] for item in kept}
    assert "semantic_material" in types
    assert "semantic_capacity" in types
    assert "semantic_feature" not in types


def test_shared_module_is_on_every_code_path():
    fact = (ROOT / "src" / "utils" / "listing_fact_sheet.py").read_text(encoding="utf-8")
    qc = (ROOT / "src" / "services" / "listing_qc.py").read_text(encoding="utf-8")
    scrub = (ROOT / "src" / "utils" / "ready_draft_claim_scrub.py").read_text(encoding="utf-8")
    audit = (ROOT / "scripts" / "audit_fix_ready_drafts.py").read_text(encoding="utf-8")
    publish = (ROOT / "batch_publish.py").read_text(encoding="utf-8")
    assert "claim_source_coverage" in fact
    assert "source_supports_claim_phrase" in fact
    assert "filter_supported_paraphrase_violations" in qc
    assert "source_supports_claim_phrase" in scrub
    assert "run_listing_qc" in audit
    assert "run_listing_qc" in publish
    assert "dry_run" in publish or "dry-run" in publish.lower()


def test_coverage_module_has_no_writes():
    source = (ROOT / "src" / "utils" / "claim_source_coverage.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    assert "execute" not in names
    assert "connect" not in names
    assert "publish_offer" not in names
    assert "write_text" not in names
    assert "open(" not in source


def test_number_words_are_hard_before_digit_conversion():
    assert is_hard_claim_phrase("two-seat sofa") is True
    assert is_hard_claim_phrase("six-person") is True
    assert is_hard_claim_phrase("four-tier") is True
    assert is_hard_claim_phrase("one-seat") is True
    assert is_hard_claim_phrase("single seater") is True
    assert is_hard_claim_phrase("padded seat") is False
    assert source_supports_claim_phrase(
        "two-seat sofa",
        "Seats: 1 Seat. Includes 2 pillows.",
    ) is False
    assert source_supports_claim_phrase("six-person", "4-6 person") is False
    assert source_supports_claim_phrase("four-tier", "3 tier shelf, 4 hooks") is False
    assert source_supports_claim_phrase("one-seat", "Seats: 1 Seat") is True


@pytest.mark.parametrize(
    "claim,source",
    [
        ("six-person", "Seats 4-6 person"),
        ("six-person", "Seats: 4-6 person"),
        ("six-person", "Seats 4-6 people"),
        ("six-person", "Seats 4 to 6 person"),
        ("four-tier", "Shelf tier 3-4 tiers"),
    ],
)
def test_range_does_not_support_exact_count_claim(claim, source):
    assert source_supports_claim_phrase(claim, source) is False
    assert ("6", "person") not in _extract_counts(source)
    assert ("4", "seat") not in _extract_counts(source)
    assert ("4", "tier") not in _extract_counts(source)


def test_extract_counts_binds_range_before_preceding_noun():
    assert _extract_counts("Seats 4-6 person") == [("4-6", "person")]
    assert _extract_counts("Seats: 4-6 person") == [("4-6", "person")]
    assert _extract_counts("Seats 4-6 people") == [("4-6", "person")]
    assert _extract_counts("Seats 4 to 6 person") == [("4-6", "person")]
    assert _extract_counts("Shelf tier 3-4 tiers") == [("3-4", "tier")]
    assert _extract_counts("Seats: 1 Seat") == [("1", "seat")]


def test_negation_and_sold_separately_are_not_support():
    assert source_supports_claim_phrase(
        "removable washable cover",
        "cover is not removable and not washable",
    ) is False
    assert source_supports_claim_phrase(
        "rechargeable battery",
        "battery not rechargeable",
    ) is False
    assert source_supports_claim_phrase("led lights", "no led lights") is False
    assert source_supports_claim_phrase("padded seat", "cushion not included") is False
    assert source_supports_claim_phrase("padded seat", "cushion sold separately") is False
    assert source_supports_claim_phrase("arms", "armless chair") is False
    assert source_supports_claim_phrase("non-slip", "anti-slip rubber pads") is True


@pytest.mark.parametrize(
    "claim,source",
    [
        ("led lights", "LED lights: none"),
        ("led lights", "LED lights: N/A"),
        ("removable cover", "Removable cover is not available"),
        ("anti-slip", "anti-slip pads not included"),
        ("anti-slip", "anti-slip mat sold separately"),
        ("padded seat", "padded seat cushion not included"),
        ("swivel", "swivels are sold separately"),
        ("swivel", "swivel base not included"),
    ],
)
def test_trailing_negation_makes_phrase_unsupported(claim, source):
    assert source_supports_claim_phrase(claim, source) is False


def test_trailing_negation_does_not_break_owner_positives():
    assert source_supports_claim_phrase("non-slip", "anti-slip rubber pads") is True
    assert source_supports_claim_phrase(
        "padded seat",
        "seat cushion with high-density resilient foam",
    ) is True
    assert source_supports_claim_phrase("one-seat", "Seats: 1 Seat") is True


def test_compounds_join_adjacent_source_tokens_only():
    assert source_supports_claim_phrase("outdoor", "door handle and pull out tray") is False
    assert source_supports_claim_phrase(
        "weatherproof",
        "weather cover, proof of purchase",
    ) is False
    assert source_supports_claim_phrase("headrest", "head cushion and a foot rest") is False
    assert source_supports_claim_phrase("tabletop", "table lamp, laptop stand") is False
    assert source_supports_claim_phrase("cupholder", "cup size, card holder") is False
    assert source_supports_claim_phrase("barrelback", "Curved Barrel back") is True


def test_multiword_claims_stay_inside_one_source_segment():
    assert source_supports_claim_phrase(
        "adjustable backrest",
        "adjustable feet, padded backrest",
    ) is False
    assert source_supports_claim_phrase(
        "with arms",
        "armless chair, arms not included",
    ) is False
    assert source_supports_claim_phrase(
        "rust-resistant",
        "water-resistant finish prevents rust",
    ) is False


def test_hard_materials_reject_faux_look_grain_qualifiers():
    assert is_hard_claim_phrase("wood frame") is True
    assert is_hard_claim_phrase("marble top") is True
    assert is_hard_claim_phrase("velvet upholstered") is True
    assert is_hard_claim_phrase("weatherproof") is True
    assert is_hard_claim_phrase("uv-resistant") is True
    assert is_hard_claim_phrase("rechargeable battery") is True
    assert source_supports_claim_phrase("wood frame", "wood grain MDF frame") is False
    assert source_supports_claim_phrase("marble top", "faux marble top") is False
    assert source_supports_claim_phrase("velvet upholstered", "velvet-look fabric") is False
    assert source_supports_claim_phrase("iron frame", "iron-look finish") is False
    assert source_supports_claim_phrase("stone top", "stone-style laminate") is False


def test_filling_does_not_cover_padded_seat():
    padded_group = next(group for group in CLAIM_PARAPHRASE_SYNONYMS if "padded" in group)
    assert "filling" not in padded_group
    assert source_supports_claim_phrase(
        "padded seat",
        "easy filling water tank seat",
    ) is False
    assert source_supports_claim_phrase(
        "padded seat",
        "seat cushion with high-density resilient foam",
    ) is True
    assert source_supports_claim_phrase(
        "high-density rebound cushioning",
        "high-density rebound filling",
    ) is True


def test_empty_claim_type_is_never_relaxed():
    kept = filter_supported_paraphrase_violations(
        [{"claim_type": "", "claim_text": "padded seat", "severity": "MEDIUM"}],
        "seat cushion with high-density resilient foam",
    )
    assert kept == [{"claim_type": "", "claim_text": "padded seat", "severity": "MEDIUM"}]
    dropped = filter_supported_paraphrase_violations(
        [{"claim_type": "semantic_feature", "claim_text": "padded seat", "severity": "MEDIUM"}],
        "seat cushion with high-density resilient foam",
    )
    assert dropped == []


def test_claim_unsupported_feature_matches_unsupported_feature():
    assert "claim_unsupported_feature" in FEATURE_CLAIM_TYPES
    assert "unsupported_feature" in FEATURE_CLAIM_TYPES
    source = "seat cushion with high-density resilient foam"
    for claim_type in ("unsupported_feature", "claim_unsupported_feature", "semantic_feature"):
        kept = filter_supported_paraphrase_violations(
            [{"claim_type": claim_type, "claim_text": "padded seat"}],
            source,
        )
        assert kept == [], claim_type
        flagged = filter_supported_paraphrase_violations(
            [{"claim_type": claim_type, "claim_text": "led lights"}],
            "no led lights",
        )
        assert flagged == [{"claim_type": claim_type, "claim_text": "led lights"}], claim_type
    item = FlaggedClaim(
        claim_type="claim_unsupported_feature",
        claim_text="padded seat",
        source_evidence="NOT_FOUND",
        location="title",
        detector="claim_diff",
        severity="HIGH",
    )
    assert item.claim_type in FEATURE_CLAIM_TYPES
    assert source_supports_claim_phrase(item.claim_text, source) is True


def test_large_source_and_many_claims_finish_under_one_second():
    source = " ".join(f"widget{index:04d}" for index in range(1500))
    violations = [
        {"claim_type": "semantic_feature", "claim_text": f"mystery feature {index}"}
        for index in range(20)
    ]
    start = time.perf_counter()
    kept = filter_supported_paraphrase_violations(violations, source)
    elapsed = time.perf_counter() - start
    assert kept == violations
    assert elapsed < 1.0


def test_review_probes_also_flag_in_factsheet():
    probes = [
        ("two-seat sofa", "Seats: 1 Seat. Includes 2 pillows."),
        ("six-person", "4-6 person"),
        ("four-tier", "3 tier shelf, 4 hooks"),
        ("removable washable cover", "cover is not removable and not washable"),
        ("rechargeable battery", "battery not rechargeable"),
        ("led lights", "no led lights"),
        ("padded seat", "cushion not included"),
        ("weatherproof", "weather cover, proof of purchase"),
        ("adjustable backrest", "adjustable feet, padded backrest"),
        ("with arms", "armless chair, arms not included"),
        ("rust-resistant", "water-resistant finish prevents rust"),
        ("wood frame", "wood grain MDF frame"),
        ("marble top", "faux marble top"),
        ("velvet upholstered", "velvet-look fabric"),
        ("padded seat", "easy filling water tank seat"),
        ("six-person", "Seats 4-6 person"),
        ("six-person", "Seats 4 to 6 person"),
        ("four-tier", "Shelf tier 3-4 tiers"),
        ("led lights", "LED lights: none"),
        ("led lights", "LED lights: N/A"),
        ("removable cover", "Removable cover is not available"),
        ("anti-slip", "anti-slip pads not included"),
        ("padded seat", "padded seat cushion not included"),
        ("swivel", "swivel base not included"),
    ]
    for claim, source in probes:
        assert _feature_violations(claim, source), (claim, source)
