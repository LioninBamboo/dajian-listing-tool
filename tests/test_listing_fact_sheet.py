import json
import sqlite3
import src.utils.listing_fact_sheet as lfs

import pytest

from src.utils.listing_fact_sheet import (
    _generic_material_supported,
    is_subjective_feature,
    compare_fact_sheets,
    fact_sheet_content_hash,
    fact_sheet_for_content,
    get_fact_sheet_cache_record,
    get_cached_fact_sheet,
    lexical_materials,
    store_fact_sheet,
)


SOURCE_SHEET = {
    "materials": ["600d oxford fabric"],
    "features": ["stove jack", "mesh windows", "roll-up sidewalls"],
    "counts": {"doors": 2, "poles": 8},
    "capacity": None,
    "certifications": [],
    "dimensions": {"length": 177.16, "width": 177.16, "height": 110.24, "weight": 65.48},
}


def test_canvas_material_hallucination_detected():
    # The W3636P456662 case: source says Oxford, live says Canvas —
    # no rule chain existed for this pair, the fact sheet diff must catch it.
    live = dict(SOURCE_SHEET, materials=["canvas"])
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    material = [v for v in violations if v["claim_type"] == "semantic_material"]
    assert material and material[0]["claim_text"] == "canvas"
    assert material[0]["severity"] == "CRITICAL"


def test_related_material_wording_not_flagged():
    # "600d oxford polyester tent fabric" shares tokens with the source
    # material — wording variation is not a hallucination.
    live = dict(SOURCE_SHEET, materials=["600d oxford tent fabric"])
    assert compare_fact_sheets(SOURCE_SHEET, live) == []


def test_unsupported_feature_detected():
    live = dict(SOURCE_SHEET, features=["stove jack", "usb charging port"])
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    features = [v for v in violations if v["claim_type"] == "semantic_feature"]
    assert features and "usb charging port" in features[0]["claim_text"]


def test_high_risk_feature_is_high_severity():
    live = dict(SOURCE_SHEET, features=["usb charging port"])
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    assert violations[0]["severity"] == "HIGH"


def test_use_case_feature_is_medium_severity():
    live = dict(SOURCE_SHEET, features=["temporary guest bed"])
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    assert violations and violations[0]["severity"] == "MEDIUM"


def test_feature_singular_plural_not_flagged():
    live = dict(SOURCE_SHEET, features=["mesh window"])
    assert compare_fact_sheets(SOURCE_SHEET, live) == []


def test_count_mismatch_detected():
    live = dict(SOURCE_SHEET, counts={"doors": 2, "poles": 12})
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    counts = [v for v in violations if v["claim_type"] == "semantic_count"]
    assert counts and "12 pole" in counts[0]["claim_text"]


def test_count_without_source_claim_not_flagged():
    # Live claims "12 stakes" but source never counts stakes — the rule-based
    # COUNTABLE_CLAIMS layer handles unknown counts; the semantic layer only
    # fires on contradictions.
    live = dict(SOURCE_SHEET, counts={"stakes": 12})
    assert compare_fact_sheets(SOURCE_SHEET, live) == []


def test_fabricated_capacity_detected():
    live = dict(SOURCE_SHEET, capacity="8 person")
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    capacity = [v for v in violations if v["claim_type"] == "semantic_capacity"]
    assert capacity and capacity[0]["severity"] == "HIGH"


def test_trivial_small_capacity_without_source_not_flagged():
    # "1 person" on a chair is the product type, not a fabricated spec.
    for trivial in ("1 person", "2 person", "2 seater"):
        live = dict(SOURCE_SHEET, capacity=trivial)
        assert compare_fact_sheets(SOURCE_SHEET, live) == [], trivial


def test_bare_generic_material_with_empty_source_not_flagged():
    source = dict(SOURCE_SHEET, materials=[])
    live = dict(SOURCE_SHEET, materials=["wood", "metal", "fabric", "foam"])
    assert compare_fact_sheets(source, live) == []


def test_bare_generic_material_with_conflicting_source_still_flagged():
    # Source explicitly says metal-family materials; live claiming "wood" is a
    # real conflict, not an unverifiable generic.
    source = dict(SOURCE_SHEET, materials=["steel", "aluminum"])
    live = dict(SOURCE_SHEET, materials=["wood"])
    violations = compare_fact_sheets(source, live)
    assert any(v["claim_type"] == "semantic_material" and v["claim_text"] == "wood" for v in violations)


def test_contradicting_capacity_is_critical():
    source = dict(SOURCE_SHEET, capacity="6 person")
    live = dict(SOURCE_SHEET, capacity="10 person")
    violations = compare_fact_sheets(source, live)
    capacity = [v for v in violations if v["claim_type"] == "semantic_capacity"]
    assert capacity and capacity[0]["severity"] == "CRITICAL"


def test_fabricated_certification_detected():
    live = dict(SOURCE_SHEET, certifications=["ul listed"])
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    certs = [v for v in violations if v["claim_type"] == "semantic_certification"]
    assert certs and certs[0]["severity"] == "CRITICAL"


def test_dimension_beyond_tolerance_detected():
    live = dict(SOURCE_SHEET, dimensions={"length": 157.2, "width": 177.16, "height": 110.24, "weight": 65.48})
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    dims = [v for v in violations if v["claim_type"] == "semantic_dimension"]
    assert dims and "157.2" in dims[0]["claim_text"]


def test_dimension_within_tolerance_not_flagged():
    live = dict(SOURCE_SHEET, dimensions={"length": 177.2, "width": 177.2, "height": 110.2, "weight": 65.5})
    assert compare_fact_sheets(SOURCE_SHEET, live) == []


def test_count_claim_present_in_source_copy_is_not_source_contradiction():
    source = dict(SOURCE_SHEET, counts={"shelves": 6})
    live = dict(SOURCE_SHEET, counts={"shelves": 3})

    violations = compare_fact_sheets(
        source,
        live,
        live_text="3 shelves",
        source_text="3-TIER corner cabinet with six cubbies",
    )

    assert not any(v["claim_type"] == "semantic_count" for v in violations)


def test_qualified_source_count_is_not_source_contradiction():
    source = dict(SOURCE_SHEET, counts={"nets": 3})
    live = dict(SOURCE_SHEET, counts={"nets": 1})

    violations = compare_fact_sheets(
        source,
        live,
        source_text="Complete set includes 1 main net and 2 side triangular nets",
    )

    assert not any(v["claim_type"] == "semantic_count" for v in violations)


def test_check_fact_sheet_ignores_package_dimensions_for_item_semantics(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "test")
    monkeypatch.setenv("AUDIT_SEMANTIC_FACT_SHEET", "1")
    source_with_package_dims = dict(
        SOURCE_SHEET,
        dimensions={"length": 29.1, "width": 7.0, "height": 16.9, "weight": 32.0},
    )
    source_without_item_dims = dict(SOURCE_SHEET, dimensions={})
    live_item_dims = dict(
        SOURCE_SHEET,
        dimensions={"length": 19.7, "width": 22.0, "height": 37.5, "weight": None},
    )
    calls = []
    descriptions = []

    def fake_fact_sheet_for_content(conn, title, description, structured):
        calls.append(dict(structured))
        descriptions.append(description)
        if len(calls) == 1:
            return source_with_package_dims if any(
                str(key).lower().startswith("package") for key in structured
            ) else source_without_item_dims
        return live_item_dims

    monkeypatch.setattr(lfs, "fact_sheet_for_content", fake_fact_sheet_for_content)

    result = lfs.check_fact_sheet_violations(
        conn=object(),
        source_title="Counter Stools",
        source_description=(
            "Two stools <ul><li>Package Length (in.): 29.1</li>"
            "<li>Package Width (in.): 7</li></ul>"
        ),
        source_attributes={},
        source_specs={
            "Package Length (in.)": "29.1",
            "Package Width (in.)": "7",
            "Package Height (in.)": "16.9",
            "Package Weight (lbs.)": "32",
        },
        candidate_title="Counter Stools",
        candidate_description="19.7 x 22 x 37.5 in",
        candidate_aspects={
            "Item Length": ["19.7 in"],
            "Item Width": ["22.0 in"],
            "Item Height": ["37.5 in"],
        },
    )

    assert result["status"] == "pass"
    assert not any(str(key).lower().startswith("package") for key in calls[0])
    assert "Package Length" not in descriptions[0]


def test_check_fact_sheet_ignores_chinese_package_dimensions_for_item_semantics(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "test")
    monkeypatch.setenv("AUDIT_SEMANTIC_FACT_SHEET", "1")
    source_with_package_dims = dict(
        SOURCE_SHEET,
        dimensions={"length": 22.8, "width": 21.6, "height": 15.3, "weight": 48.89},
    )
    source_without_item_dims = dict(SOURCE_SHEET, dimensions={})
    live_item_dims = dict(
        SOURCE_SHEET,
        dimensions={"length": 21.45, "width": 21.45, "height": 42.32, "weight": None},
    )
    descriptions = []

    def fake_fact_sheet_for_content(conn, title, description, structured):
        descriptions.append(description)
        return source_with_package_dims if "包装尺寸" in description else (
            source_without_item_dims if len(descriptions) == 1 else live_item_dims
        )

    monkeypatch.setattr(lfs, "fact_sheet_for_content", fake_fact_sheet_for_content)

    result = lfs.check_fact_sheet_violations(
        conn=object(),
        source_title="Hair Beauty Salon Equipment Black Hydraulic Barber Styling Chair",
        source_description=(
            "<div>包装尺寸 长度 22.8 宽度 21.6 高度 15.3</div>"
            "<div>产品特点 Hydraulic pump</div>"
            "<ul><li><strong>Package Length (in.):</strong> 22.8</li>"
            "<li><strong>Package Height (in.):</strong> 15.3</li></ul>"
        ),
        source_attributes={"Main Material": "PU"},
        source_specs={
            "Package Length (in.)": "22.8",
            "Package Width (in.)": "21.6",
            "Package Height (in.)": "15.3",
        },
        candidate_title="Hair Beauty Salon Equipment Black Hydraulic Barber Styling Chair",
        candidate_description="21.45 x 21.45 x 42.32 in",
        candidate_aspects={
            "Item Length": ["21.45 in"],
            "Item Width": ["21.45 in"],
            "Item Height": ["42.32 in"],
        },
    )

    assert result["status"] == "pass"
    assert "包装尺寸" not in descriptions[0]


def test_check_fact_sheet_ignores_labeled_seat_height_for_item_semantics(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "test")
    monkeypatch.setenv("AUDIT_SEMANTIC_FACT_SHEET", "1")
    source_with_seat_height = dict(SOURCE_SHEET, dimensions={"height": 26.0})
    source_without_item_dims = dict(SOURCE_SHEET, dimensions={})
    live_item_dims = dict(SOURCE_SHEET, dimensions={"height": 37.5})
    titles = []
    descriptions = []

    def fake_fact_sheet_for_content(conn, title, description, structured):
        titles.append(title)
        descriptions.append(description)
        if len(titles) == 1:
            return (
                source_with_seat_height
                if "seat height" in f"{title} {description}".lower()
                else source_without_item_dims
            )
        return live_item_dims

    monkeypatch.setattr(lfs, "fact_sheet_for_content", fake_fact_sheet_for_content)

    result = lfs.check_fact_sheet_violations(
        conn=object(),
        source_title="Swivel Counter Stools, 26 Inch Seat Height",
        source_description="Two stools with a 26-inch seat height.",
        source_attributes={},
        source_specs={},
        candidate_title="Swivel Counter Stools",
        candidate_description="Overall height 37.5 in",
        candidate_aspects={"Item Height": ["37.5 in"]},
    )

    assert result["status"] == "pass"
    assert "Seat Height" not in titles[0]
    assert "seat height" not in descriptions[0].lower()


def test_identical_sheets_no_violations():
    assert compare_fact_sheets(SOURCE_SHEET, dict(SOURCE_SHEET)) == []


def test_four_season_supported_via_year_round_synonym():
    source = dict(SOURCE_SHEET, features=SOURCE_SHEET["features"] + ["year-round use"])
    live = dict(SOURCE_SHEET, features=["4 season"])
    assert compare_fact_sheets(source, live) == []


def test_four_season_unsupported_without_source_evidence():
    live = dict(SOURCE_SHEET, features=["4 season"])
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    assert any(v["claim_type"] == "semantic_feature" and "4 season" in v["claim_text"] for v in violations)


def test_drop_leaf_supports_foldable_feature():
    source = dict(SOURCE_SHEET, features=["drop leaf", "storage"])
    live = dict(SOURCE_SHEET, features=["foldable"])
    assert compare_fact_sheets(source, live) == []


def test_hydraulic_adjustment_supports_adjustable_height():
    source = dict(SOURCE_SHEET, features=["hydraulic adjustment"])
    live = dict(SOURCE_SHEET, features=["adjustable height"])
    assert compare_fact_sheets(source, live) == []


def test_capacity_person_vs_seat_equivalent():
    source = dict(SOURCE_SHEET, capacity="3 seat")
    live = dict(SOURCE_SHEET, capacity="3 person")
    assert compare_fact_sheets(source, live) == []


def test_capacity_within_source_range_supported():
    source = dict(SOURCE_SHEET, capacity="4-8 person")
    live = dict(SOURCE_SHEET, capacity="8 person")
    assert compare_fact_sheets(source, live) == []


def test_capacity_above_source_range_flagged():
    source = dict(SOURCE_SHEET, capacity="4-8 person")
    live = dict(SOURCE_SHEET, capacity="10 person")
    violations = compare_fact_sheets(source, live)
    assert any(v["claim_type"] == "semantic_capacity" and v["severity"] == "CRITICAL" for v in violations)


def test_explicit_source_bed_size_claim_suppresses_capacity_conflict():
    """A source bed-size claim can conflict with a structured occupancy field."""
    source = dict(SOURCE_SHEET, capacity="2 person")
    live = dict(SOURCE_SHEET, capacity="twin size")
    assert compare_fact_sheets(
        source,
        live,
        source_text="Outdoor twin size swing bed",
    ) == []


def test_generic_fabric_backed_by_specific_source_material():
    source = dict(SOURCE_SHEET, materials=["corduroy", "foam"])
    live = dict(SOURCE_SHEET, materials=["fabric", "foam"])
    assert compare_fact_sheets(source, live) == []


def test_specific_fabric_not_in_source_still_flagged():
    source = dict(SOURCE_SHEET, materials=["corduroy", "foam"])
    live = dict(SOURCE_SHEET, materials=["polyester"])
    violations = compare_fact_sheets(source, live)
    assert any(v["claim_type"] == "semantic_material" and v["claim_text"] == "polyester" for v in violations)


def test_generic_metal_backed_by_steel():
    source = dict(SOURCE_SHEET, materials=["steel", "acacia wood"])
    live = dict(SOURCE_SHEET, materials=["metal"])
    assert compare_fact_sheets(source, live) == []


def test_duplicate_claim_across_buckets_reported_once_as_critical():
    live = dict(SOURCE_SHEET, features=["ul certified"], certifications=["ul certified"])
    violations = compare_fact_sheets(SOURCE_SHEET, live)
    ul = [v for v in violations if "ul certified" in v["claim_text"]]
    assert len(ul) == 1 and ul[0]["severity"] == "CRITICAL"


def test_lexical_materials_catches_title_material():
    # The exact W3636 miss: qwen read "Canvas Bell Tent" as a product type.
    found = lexical_materials("TITLE: Luxury Canvas Bell Tent with Stove Jack")
    assert "canvas" in found


def test_lexical_materials_longest_match_wins():
    found = lexical_materials("frame made of stainless steel and tempered glass top")
    assert "stainless steel" in found and "tempered glass" in found
    assert "steel" not in found and "glass" not in found


def test_lexical_materials_word_boundaries():
    # "abs" must not fire inside "absorb"; "iron" must not fire inside "environment"
    assert lexical_materials("shock absorbing pads for your environment") == set()


def test_content_hash_stable_and_sensitive():
    h1 = fact_sheet_content_hash("Title", "<p>Desc</p>", {"Material": "Oxford"})
    h2 = fact_sheet_content_hash("Title", "<p>Desc</p>", {"Material": "Oxford"})
    h3 = fact_sheet_content_hash("Title", "<p>Desc changed</p>", {"Material": "Oxford"})
    assert h1 == h2
    assert h1 != h3


@pytest.fixture()
def cache_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "cache.db")
    yield conn
    conn.close()


def test_cache_round_trip(cache_conn):
    key = fact_sheet_content_hash("T", "D", {})
    assert get_cached_fact_sheet(cache_conn, key) is None
    store_fact_sheet(cache_conn, key, SOURCE_SHEET)
    assert get_cached_fact_sheet(cache_conn, key) == SOURCE_SHEET


def test_cache_round_trip_records_current_versions_and_provenance(cache_conn):
    key = fact_sheet_content_hash("T-provenance", "D", {})
    store_fact_sheet(
        cache_conn,
        key,
        SOURCE_SHEET,
        source_kind="supplier_source",
        model_status="extracted",
    )

    record = get_fact_sheet_cache_record(cache_conn, key)

    assert record["sheet"] == SOURCE_SHEET
    assert record["fact_sheet_version"] == lfs.FACT_SHEET_VERSION
    assert record["ruleset_version"] == lfs.FACT_SHEET_RULESET_VERSION
    assert record["source_kind"] == "supplier_source"
    assert record["model_status"] == "extracted"


def test_cache_ignores_records_from_an_old_ruleset(cache_conn, monkeypatch):
    key = fact_sheet_content_hash("T-version", "D", {})
    store_fact_sheet(cache_conn, key, SOURCE_SHEET)
    monkeypatch.setattr(lfs, "FACT_SHEET_RULESET_VERSION", "fact-sheet-rules-v2")

    assert get_cached_fact_sheet(cache_conn, key) is None


def test_cache_migrates_legacy_table_without_treating_legacy_rows_as_current(cache_conn):
    cache_conn.execute(
        "CREATE TABLE fact_sheet_cache (content_hash TEXT PRIMARY KEY, sheet_json TEXT NOT NULL, created_at TEXT)"
    )
    key = fact_sheet_content_hash("T-legacy", "D", {})
    cache_conn.execute(
        "INSERT INTO fact_sheet_cache (content_hash, sheet_json) VALUES (?, ?)",
        (key, json.dumps(SOURCE_SHEET)),
    )
    cache_conn.commit()

    assert get_cached_fact_sheet(cache_conn, key) is None
    store_fact_sheet(cache_conn, key, SOURCE_SHEET, source_kind="legacy-migrated")
    record = get_fact_sheet_cache_record(cache_conn, key)
    assert record["source_kind"] == "legacy-migrated"


def test_fact_sheet_for_content_uses_cache(cache_conn, monkeypatch):
    key = fact_sheet_content_hash("T", "D", {"A": "1"})
    store_fact_sheet(cache_conn, key, SOURCE_SHEET)

    def _boom(*_args, **_kwargs):
        raise AssertionError("LLM must not be called on cache hit")

    monkeypatch.setattr("src.utils.listing_fact_sheet.extract_fact_sheet", _boom)
    sheet = fact_sheet_for_content(cache_conn, "T", "D", {"A": "1"})
    assert sheet == SOURCE_SHEET


def test_fact_sheet_for_content_extracts_and_stores_on_miss(cache_conn, monkeypatch):
    monkeypatch.setattr(
        "src.utils.listing_fact_sheet.extract_fact_sheet",
        lambda *args, **kwargs: dict(SOURCE_SHEET),
    )
    sheet = fact_sheet_for_content(cache_conn, "T2", "D2", {})
    assert sheet["materials"] == ["600d oxford fabric"]
    key = fact_sheet_content_hash("T2", "D2", {})
    assert get_cached_fact_sheet(cache_conn, key) is not None


def test_fact_sheet_for_content_uses_explicit_item_aspects_for_dimensions(cache_conn, monkeypatch):
    extracted = dict(
        SOURCE_SHEET,
        dimensions={"length": 33.86, "width": 33.86, "height": 15.16, "weight": None},
    )
    monkeypatch.setattr(
        "src.utils.listing_fact_sheet.extract_fact_sheet",
        lambda *args, **kwargs: extracted,
    )

    sheet = fact_sheet_for_content(
        cache_conn,
        "Nesting Coffee Tables",
        "33.86 x 18.9 x 15.16 in",
        {
            "Item Length": ["33.86 in"],
            "Item Width": ["18.9 in"],
            "Item Height": ["15.16 in"],
        },
    )

    assert sheet["dimensions"] == {
        "length": 33.86,
        "width": 18.9,
        "height": 15.16,
        "weight": None,
    }


def test_fact_sheet_for_content_uses_assembled_source_dimensions_when_title_has_no_size(
    cache_conn, monkeypatch
):
    extracted = dict(
        SOURCE_SHEET,
        dimensions={"length": 18.9, "width": 33.86, "height": 15.16, "weight": None},
    )
    monkeypatch.setattr(
        "src.utils.listing_fact_sheet.extract_fact_sheet",
        lambda *args, **kwargs: extracted,
    )

    sheet = fact_sheet_for_content(
        cache_conn,
        "Nesting Coffee Tables",
        "33.86 x 18.9 x 15.16 in",
        {
            "Assembled Length (in.)": "33.86",
            "Assembled Width (in.)": "18.90",
            "Assembled Height (in.)": "15.16",
        },
    )

    assert sheet["dimensions"]["length"] == 33.86
    assert sheet["dimensions"]["width"] == 18.9


def test_fact_sheet_for_content_keeps_conflicting_title_length_for_review(cache_conn, monkeypatch):
    extracted = dict(
        SOURCE_SHEET,
        dimensions={"length": 100.0, "width": 91.34, "height": 34.65, "weight": None},
    )
    monkeypatch.setattr(
        "src.utils.listing_fact_sheet.extract_fact_sheet",
        lambda *args, **kwargs: extracted,
    )

    sheet = fact_sheet_for_content(
        cache_conn,
        "100-inch Dining Table",
        "Large dining table",
        {
            "Assembled Length (in.)": "114.57",
            "Assembled Width (in.)": "91.34",
            "Assembled Height (in.)": "34.65",
        },
    )

    assert sheet["dimensions"] == {
        "length": 100.0,
        "width": 91.34,
        "height": 34.65,
        "weight": None,
    }


def test_fact_sheet_for_content_uses_assembled_sofa_length_over_size_class_title(
    cache_conn, monkeypatch
):
    extracted = dict(
        SOURCE_SHEET,
        dimensions={"length": 100.0, "width": 91.34, "height": 34.65, "weight": None},
    )
    monkeypatch.setattr(
        "src.utils.listing_fact_sheet.extract_fact_sheet",
        lambda *args, **kwargs: extracted,
    )

    sheet = fact_sheet_for_content(
        cache_conn,
        "100-inch Pull-Out Sofa",
        "Large sectional sofa",
        {
            "Assembled Length (in.)": "114.57",
            "Assembled Width (in.)": "91.34",
            "Assembled Height (in.)": "34.65",
        },
    )

    assert sheet["dimensions"]["length"] == 114.57


def test_explicit_dimensions_normalize_hall_tree_source_axes():
    values = lfs._explicit_fact_sheet_dimensions(
        {
            "Product Type": "Hall Tree",
            "Assembled Length (in.)": "76.70",
            "Assembled Width (in.)": "59.00",
            "Assembled Height (in.)": "15.70",
        },
        "Hall Tree with Storage Bench",
    )

    assert values == {"length": 15.7, "width": 59.0, "height": 76.7}


def test_explicit_dimensions_keep_hall_tree_title_height_axis():
    values = lfs._explicit_fact_sheet_dimensions(
        {
            "Product Type": "Hall Tree",
            "Assembled Length (in.)": "76.70",
            "Assembled Width (in.)": "59.00",
            "Assembled Height (in.)": "15.70",
        },
        'Farmhouse Wooden Hall Tree, 76.7"H',
    )

    assert values == {"length": 15.7, "width": 59.0, "height": 76.7}


def test_zero_cached_source_counts_are_treated_as_missing():
    source = dict(SOURCE_SHEET, counts={"shelves": 0, "drawers": 0})
    live = dict(SOURCE_SHEET, counts={"shelves": 3, "drawers": 2})

    violations = compare_fact_sheets(source, live)

    assert not any(v["claim_type"] == "semantic_count" for v in violations)


def test_explicit_zero_source_count_is_still_compared():
    source = dict(SOURCE_SHEET, counts={"drawers": 0})
    live = dict(SOURCE_SHEET, counts={"drawers": 2})

    violations = compare_fact_sheets(source, live, source_text="The unit has 0 drawers.")

    assert any(v["claim_type"] == "semantic_count" for v in violations)


def test_structured_capacity_wins_over_conflicting_source_copy(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "test")
    monkeypatch.setenv("AUDIT_SEMANTIC_FACT_SHEET", "1")
    calls = []

    def fake_fact_sheet_for_content(conn, title, description, structured):
        calls.append(dict(structured))
        return dict(SOURCE_SHEET, capacity="5 person" if len(calls) == 1 else "4 person")

    monkeypatch.setattr(lfs, "fact_sheet_for_content", fake_fact_sheet_for_content)

    result = lfs.check_fact_sheet_violations(
        conn=object(),
        source_title="Modern Sectional Sofa",
        source_description="The source copy says it can seat up to 5 people.",
        source_attributes={"Seats": "4 Seat"},
        source_specs={},
        candidate_title="Modern Sectional Sofa",
        candidate_description="The sectional offers generous seating space.",
        candidate_aspects={"Seating Capacity": ["4"]},
    )

    assert result["status"] == "pass"


def test_fact_sheet_for_content_returns_none_when_llm_fails(cache_conn, monkeypatch):
    monkeypatch.setattr(
        "src.utils.listing_fact_sheet.extract_fact_sheet",
        lambda *args, **kwargs: None,
    )
    assert fact_sheet_for_content(cache_conn, "T3", "D3", {}) is None
    key = fact_sheet_content_hash("T3", "D3", {})
    assert get_cached_fact_sheet(cache_conn, key) is None


class TestGroundingGuard:
    """描述层材质债根因修复：grounding —— live_text/source_text 落地要求。
    核心不变量：真幻觉(词真在描述里且源不支持)绝不被豁免。"""

    BASE = {
        "materials": ["fabric"], "features": [], "counts": {}, "capacity": None,
        "certifications": [], "dimensions": {},
    }

    def _src(self, materials, cert=None):
        s = dict(self.BASE); s["materials"] = materials
        if cert: s["certifications"] = cert
        return s

    def test_C_extractor_inferred_material_not_in_live_text_suppressed(self):
        # 提取器脑补出 foam,但 live 文案根本没提 foam → 豁免
        src = self._src(["fabric", "wood"])
        live = {**self.BASE, "materials": ["foam"]}
        v = compare_fact_sheets(src, live, live_text="Comfortable cushioned sofa with wood legs")
        assert not any(x["claim_type"] == "semantic_material" for x in v)

    def test_C_extractor_inferred_feature_not_in_live_text_suppressed(self):
        # FactSheet may infer foldable from a swing/rope design, but an inferred
        # feature is not a live claim unless it appears in copy or aspects.
        src = self._src(["acacia wood"])
        live = {**self.BASE, "features": ["foldable"]}
        v = compare_fact_sheets(
            src,
            live,
            live_text="Acacia wood swing bed with hanging ropes",
            source_text="Acacia wood, two person seating group",
        )
        assert not any(x["claim_type"] == "semantic_feature" for x in v)

    def test_C_real_hallucination_in_live_text_still_flagged(self):
        # canvas 真在描述里、源是 rubberwood → 必须仍报(不可被 grounding 放过)
        src = self._src(["rubberwood", "walnut"])
        live = {**self.BASE, "materials": ["canvas"]}
        v = compare_fact_sheets(src, live, live_text="1 x canvas wardrobe with walnut finish",
                                source_text="rubberwood solid rubberwood walnut")
        assert any(x["claim_type"] == "semantic_material" and x["claim_text"] == "canvas" for x in v)

    def test_A_material_literally_in_source_text_suppressed(self):
        # melamine 在源文案里真有,只是源 sheet 提取漏了 → 豁免
        src = self._src(["mdf"])
        live = {**self.BASE, "materials": ["melamine"]}
        v = compare_fact_sheets(src, live, live_text="melamine coated surface",
                                source_text="MDF board with melamine coating")
        assert not any(x["claim_type"] == "semantic_material" for x in v)

    def test_A_powder_coated_supported_by_source_powder_coating(self):
        # W1586 trellis residual CRITICAL: live/extractor "powder coated",
        # source says "powder coating surface treatment" (same finish).
        src = self._src(["iron"])
        live = {**self.BASE, "materials": ["powder coated", "iron"]}
        v = compare_fact_sheets(
            src,
            live,
            live_text="Metal galvanized tube with powder coated finish",
            source_text="Sturdy Material: Metal galvanized tube with powder coating surface treatment",
        )
        assert not any(
            x["claim_type"] == "semantic_material" and "powder" in x["claim_text"]
            for x in v
        )

    def test_token_disjoint_hallucination_survives_grounding(self):
        # 源 plastic,描述写 bamboo(无共享 token、真在描述里、源无) → 必须报,
        # grounding 不能因为"在描述里"就放过它
        src = self._src(["plastic"])
        live = {**self.BASE, "materials": ["bamboo"]}
        v = compare_fact_sheets(src, live, live_text="Solid bamboo construction",
                                source_text="ABS plastic housing")
        assert any(x["claim_type"] == "semantic_material" and x["claim_text"] == "bamboo" for x in v)

    def test_fake_certification_in_live_text_still_flagged(self):
        src = self._src(["steel"])
        live = {**self.BASE, "materials": ["steel"], "certifications": ["ul certified"]}
        v = compare_fact_sheets(src, live, live_text="UL Certified steel frame",
                                source_text="steel frame")
        assert any(x["claim_type"] == "semantic_certification" for x in v)

    def test_inferred_certification_not_in_text_suppressed(self):
        src = self._src(["steel"])
        live = {**self.BASE, "materials": ["steel"], "certifications": ["ul certified"]}
        v = compare_fact_sheets(src, live, live_text="Sturdy steel frame for daily use",
                                source_text="steel frame")
        assert not any(x["claim_type"] == "semantic_certification" for x in v)

    def test_legacy_behavior_unchanged_without_text(self):
        # 不传 text → 完全保持旧行为(向后兼容)
        src = self._src(["fabric", "wood"])
        live = {**self.BASE, "materials": ["foam"]}
        v = compare_fact_sheets(src, live)
        assert any(x["claim_type"] == "semantic_material" for x in v)


class TestEngineeredWoodAndPolyethyleneFamilies:
    """2026-07-26 审计:207 条 CRITICAL 里 21 条是材质家族统称被误判。
    "engineered wood" 就是 MDF/刨花板/三聚氰胺板的美式零售统称,HDPE 就是
    PE rattan 的材质。但放行必须要求源里确有该家族的**具体成员**——纯软体
    沙发(chenille/foam)claim "engineered wood frame" 是真幻觉,必须继续拦。"""

    @pytest.mark.parametrize("claimed,source_materials", [
        ("engineered wood", ["glass", "mdf", "particle board", "particleboard"]),
        ("engineered wood", ["mdf", "melamine", "particle board"]),
        ("engineered wood", ["mdf"]),
        ("hdpe", ["foam", "iron", "pe rattan", "polyester"]),
        ("polyethylene", ["pe rattan", "steel"]),
    ])
    def test_family_umbrella_backed_by_member_is_supported(self, claimed, source_materials):
        assert _generic_material_supported(claimed, source_materials) is True

    @pytest.mark.parametrize("claimed,source_materials,why", [
        ("engineered wood", ["chenille"], "W2519P486547 纯软体沙发,源无任何板材"),
        ("engineered wood", ["chenille", "foam"], "W2519P486112"),
        ("engineered wood", ["corduroy", "foam"], "W714S01469"),
        ("engineered wood frame", ["boucle", "foam"], "W5532P458419 编造框架材质"),
        ("hdpe", ["300d polyester fabric", "polyester", "pu"], "W2505P427760 涤纶帐篷"),
        ("polyethylene", ["polyester", "pu"], "同上"),
    ])
    def test_family_umbrella_without_member_still_flags(self, claimed, source_materials, why):
        assert _generic_material_supported(claimed, source_materials) is False, why


class TestSubjectiveFeatureFilter:
    """2026-07-26:审计每轮报 2896 条 MEDIUM,真 CRITICAL 只有 207,被淹没。
    其中一批是无法证伪的主观词——源规格永远不可能反驳"modern"/"sturdy"。
    但安全/功能类措辞必须继续检查(买家会因此退款)。"""

    @pytest.mark.parametrize("feature", [
        "comfortable", "modern", "sturdy", "ergonomic", "space-saving",
        "lightweight", "easy clean", "easy to assemble", "multi-functional",
        "indoor", "premium", "elegant", "compact",
    ])
    def test_unfalsifiable_wording_is_skipped(self, feature):
        assert is_subjective_feature(feature) is True

    @pytest.mark.parametrize("feature", [
        "waterproof", "foldable", "adjustable", "reclining", "locking",
        "usb charging port", "with storage", "buckle connection",
        "scratch resistant", "800 lbs capacity", "stove jack",
    ])
    def test_verifiable_claims_still_checked(self, feature):
        assert is_subjective_feature(feature) is False

    def test_high_risk_wording_wins_over_subjective_prefix(self):
        # "easy to clean waterproof cover" 含主观前缀但核心是可证伪的防水声明
        assert is_subjective_feature("easy to clean waterproof cover") is False


class TestCapacityEquivalentPhrasings:
    """2026-07-27 存量分析:12 条 semantic_capacity CRITICAL 里 8 条是同义表述
    被当成矛盾——"2 person" vs 源 "two-seater"、"queen size" vs 源 "queen"。
    拼写数字解析不出数值就退化成 token 比对,"size" 是纯噪声词。"""

    def test_spelled_out_count_equals_digit(self):
        assert lfs._capacity_supported("2 person", "two-seater") is True
        assert lfs._capacity_supported("two-seater", "2 person") is True

    def test_size_is_a_noise_word(self):
        assert lfs._capacity_supported("queen size", "queen") is True
        assert lfs._capacity_supported("full size", "full") is True

    def test_mattress_size_implies_occupancy(self):
        assert lfs._capacity_supported("queen", "2 person") is True
        assert lfs._capacity_supported("full", "2 person") is True
        assert lfs._capacity_supported("twin", "1 person") is True

    def test_longest_size_name_wins(self):
        # "california king"/"twin xl" 必须胜过它们内含的 "king"/"twin"
        assert lfs._capacity_numbers("california king") == (2, 2)
        assert lfs._capacity_numbers("twin xl") == (1, 1)

    def test_genuine_capacity_conflicts_still_report(self):
        # 这 4 条用户明确要留给人工,不许被同义词表吞掉
        assert lfs._capacity_supported("6 seater", "4 person") is False
        assert lfs._capacity_supported("king size", "4 person") is False
        assert lfs._capacity_supported("king size", "4 seater") is False
        assert lfs._capacity_supported("8 person", "4 person") is False

    def test_range_containment_unchanged(self):
        assert lfs._capacity_supported("6 person", "4-8 person") is True
        assert lfs._capacity_supported("10 person", "4-8 person") is False
