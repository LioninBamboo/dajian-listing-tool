"""Regression coverage for source-grounded publish aspect behavior."""

from __future__ import annotations

import batch_publish
from src.utils.publish_fee_preview import (
    evaluate_insertion_fee_preview,
    load_high_fee_blacklist_category_paths,
    load_high_fee_category_blacklist,
)
from src.utils.publish_source_aspect_guard import (
    build_giga_source_text,
    find_source_matching_value,
    guard_unsourced_aspects,
    is_material_aspect,
    match_allowed_whole_value,
    merge_aspects_for_draft_persist,
    restore_sourced_aspects,
    value_supported_by_source,
)

_TYPE_ALLOWED = ["Accent Chair", "Armchair", "Sofa", "Sectional"]
_TYPE_SOURCE = "Modern Accent Chair with metal frame. Product Type: Accent Chair."


def test_infer_upholstery_fabric_does_not_invent_a_specific_fiber():
    from src.utils.publish_aspect_completion import infer_upholstery_fabric

    assert infer_upholstery_fabric("Foam Cushion Ottoman", {"Material": "Foam"}) == "Fabric"
    assert infer_upholstery_fabric("Chenille Sofa", {}) == "Chenille"
    assert infer_upholstery_fabric("Microsuede Recliner", {}) == "Microfiber"


def test_specialized_furniture_categories_are_protected_from_generic_remap():
    from src.services.taxonomy_constants import PROTECTED_STORED_CATEGORY_IDS

    assert {"20689", "261263"}.issubset(PROTECTED_STORED_CATEGORY_IDS)


def test_apothecary_cabinet_template_default_is_dropped_when_optional():
    logs = []
    aspects = {"Type": ["Apothecary Cabinet"], "Color": ["White"]}
    source = build_giga_source_text(
        title="Bathroom Vanity with Sink",
        description="A compact bathroom vanity with ceramic basin.",
    )

    blockers = guard_unsourced_aspects(
        aspects,
        source_text=source,
        required_names=[],
        sku="SKU-VANITY",
        log=logs.append,
    )

    assert blockers == []
    assert "Type" not in aspects
    assert any("Apothecary Cabinet" in line and "[ASPECT_STRIP]" in line for line in logs)


def test_apothecary_cabinet_required_without_source_match_blocks():
    aspects = {"Type": ["Apothecary Cabinet"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Generic household widget with no furniture type",
        required_names=["Type"],
        allowed_values_by_name={"Type": ["Apothecary Cabinet"]},
        sku="SKU-WIDGET",
    )

    assert "unsourced_required_aspect:Type" in blockers
    assert "Type" not in aspects


def test_sourced_aspect_is_kept_and_not_overwritten_by_template():
    source = build_giga_source_text(
        title="Bathroom Vanity",
        description="Silver glass bathroom vanity cabinet.",
        attributes={"Color": "Silver", "Material": "Glass"},
    )
    completed = {"Type": ["Apothecary Cabinet"], "Color": ["Red"], "Material": ["Wood"]}
    restore_sourced_aspects(
        completed,
        {"Type": ["Bathroom Vanity"], "Color": ["Silver"], "Material": ["Glass"]},
        source,
    )

    assert completed["Type"] == ["Bathroom Vanity"]
    assert completed["Color"] == ["Silver"]
    assert completed["Material"] == ["Glass"]

    blockers = guard_unsourced_aspects(
        completed,
        source_text=source,
        required_names=["Type", "Color", "Material"],
    )
    assert blockers == []
    assert completed["Type"] == ["Bathroom Vanity"]
    assert completed["Color"] == ["Silver"]
    assert completed["Material"] == ["Glass"]


def test_missing_required_aspect_blocks_when_source_has_no_match():
    aspects = {"Brand": ["AquaVerve"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Plain household widget",
        required_names=["Type"],
    )
    assert blockers == ["unsourced_required_aspect:Type"]


def test_required_unsourced_color_uses_source_matching_value():
    aspects = {"Color": ["Red"]}
    source = "Silver glass wall mirror"
    assert find_source_matching_value("Color", source, ["Red", "Silver", "Black"]) == "Silver"

    blockers = guard_unsourced_aspects(
        aspects,
        source_text=source,
        required_names=["Color"],
        allowed_values_by_name={"Color": ["Red", "Silver", "Black"]},
    )
    assert blockers == []
    assert aspects["Color"] == ["Silver"]


def test_unsourced_voltage_aspect_is_always_dropped():
    aspects = {"Voltage": ["110V"], "Type": ["Kitchen Island"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Kitchen island with wood top and storage drawers",
        required_names=[],
    )
    assert blockers == []
    assert "Voltage" not in aspects
    assert aspects["Type"] == ["Kitchen Island"]


def test_mpn_does_not_apply_is_kept():
    aspects = {"MPN": ["Does Not Apply"], "Brand": ["Unbranded"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Upholstered storage ottoman with lid",
        required_names=[],
    )
    assert blockers == []
    assert aspects["MPN"] == ["Does Not Apply"]
    assert aspects["Brand"] == ["Unbranded"]


def test_assembly_required_kept_when_gate_expects_it():
    aspects = {"Assembly Required": ["Yes"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Upholstered storage ottoman with lid",
        expected_values_by_name={"Assembly Required": "Yes"},
    )
    assert blockers == []
    assert aspects["Assembly Required"] == ["Yes"]


def test_type_kept_when_it_matches_gate_expectation():
    aspects = {"Type": ["Storage Ottoman"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Upholstered ottoman for the living room",
        required_names=["Type"],
        allowed_values_by_name={"Type": ["Ottoman", "Storage Ottoman"]},
        expected_values_by_name={"Type": "Storage Ottoman"},
    )
    assert blockers == []
    assert aspects["Type"] == ["Storage Ottoman"]


def test_match_allowed_whole_value_equality_not_substring():
    assert match_allowed_whole_value("Accent Chair", _TYPE_ALLOWED) == "Accent Chair"
    assert match_allowed_whole_value("  accent chair ", _TYPE_ALLOWED) == "Accent Chair"
    assert match_allowed_whole_value("Accent", _TYPE_ALLOWED) is None
    assert match_allowed_whole_value("Chair", _TYPE_ALLOWED) is None
    assert match_allowed_whole_value("accent-chair", _TYPE_ALLOWED) == "Accent Chair"
    assert match_allowed_whole_value("", _TYPE_ALLOWED) is None
    assert match_allowed_whole_value(None, _TYPE_ALLOWED) is None
    assert match_allowed_whole_value("Accent Chair", None) is None
    assert match_allowed_whole_value("Accent Chair", []) is None


def test_draft_type_accent_chair_wins_over_inferred_armchair():
    aspects = {"Type": ["Armchair"], "Brand": ["AquaVerve"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=_TYPE_SOURCE,
        required_names=["Type"],
        allowed_values_by_name={"Type": _TYPE_ALLOWED},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={"Type": ["Accent Chair"]},
        expected_values_by_name={"Type": "Armchair"},
    )
    assert blockers == []
    assert aspects["Type"] == ["Accent Chair"]


def test_empty_draft_type_allows_expected_autofill_armchair():
    aspects = {"Brand": ["AquaVerve"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=_TYPE_SOURCE,
        required_names=["Type"],
        allowed_values_by_name={"Type": _TYPE_ALLOWED},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={"Brand": ["AquaVerve"]},
        expected_values_by_name={"Type": "Armchair"},
    )
    assert blockers == []
    assert aspects["Type"] == ["Armchair"]


def test_draft_type_whitespace_case_normalizes_to_allowed_value():
    aspects = {"Type": ["Armchair"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=_TYPE_SOURCE,
        required_names=["Type"],
        allowed_values_by_name={"Type": _TYPE_ALLOWED},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={"Type": ["  accent chair "]},
        expected_values_by_name={"Type": "Armchair"},
    )
    assert blockers == []
    assert aspects["Type"] == ["Accent Chair"]


def test_draft_type_prefix_does_not_match_allowed_and_blocks():
    aspects = {"Type": ["Armchair"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=_TYPE_SOURCE,
        required_names=["Type"],
        allowed_values_by_name={"Type": _TYPE_ALLOWED},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={"Type": ["Accent"]},
        expected_values_by_name={"Type": "Accent Chair"},
    )
    assert blockers == ["aspect_value_not_allowed:Type=Accent"]
    # Invalid draft does not wipe upstream; SKU still blocked.
    assert aspects["Type"] == ["Armchair"]


def test_free_text_draft_value_not_blocked_by_allowed_list():
    """FREE_TEXT must not apply SELECTION_ONLY blocking; unsourced draft keeps upstream."""
    aspects = {"Type": ["Accent Chair"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=_TYPE_SOURCE,
        required_names=["Type"],
        allowed_values_by_name={"Type": _TYPE_ALLOWED},
        aspect_modes_by_name={"Type": "FREE_TEXT"},
        draft_aspects={"Type": ["Accent Chair Custom"]},
        expected_values_by_name={"Type": "Armchair"},
    )
    assert all(not item.startswith("aspect_value_not_allowed:") for item in blockers)
    # Unsourced FREE_TEXT draft is not seeded over sourced upstream Accent Chair.
    assert blockers == []
    assert aspects["Type"] == ["Accent Chair"]


def test_missing_required_type_still_blocks_when_no_draft_and_no_inference():
    aspects = {"Brand": ["AquaVerve"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Plain household widget with no furniture type",
        required_names=["Type"],
        allowed_values_by_name={"Type": _TYPE_ALLOWED},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={},
        expected_values_by_name={},
    )
    assert blockers == ["unsourced_required_aspect:Type"]
    assert "Type" not in aspects


def test_draft_material_wins_over_inferred_wood():
    material_allowed = ["MDF/Chipboard", "Solid Wood", "Wood", "Metal"]
    source = build_giga_source_text(
        title="Kitchen Island Cabinet",
        description="MDF cabinet body with storage.",
        attributes={"Material": "MDF/Chipboard"},
    )
    aspects = {"Material": ["Wood"], "Type": ["Kitchen Island"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=source,
        required_names=["Material"],
        allowed_values_by_name={"Material": material_allowed},
        aspect_modes_by_name={"Material": "SELECTION_ONLY"},
        draft_aspects={"Material": ["MDF/Chipboard"], "Type": ["Kitchen Island"]},
        expected_values_by_name={},
        source_fields={"Material": "MDF/Chipboard"},
    )
    assert blockers == []
    assert aspects["Material"] == ["MDF/Chipboard"]


def test_autofill_precedence_expected_before_source_match_when_draft_empty():
    """Empty draft: expected_values win over find_source_matching_value."""
    aspects = {}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=_TYPE_SOURCE,
        required_names=["Type"],
        allowed_values_by_name={"Type": _TYPE_ALLOWED},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={},
        expected_values_by_name={"Type": "Armchair"},
    )
    assert blockers == []
    assert aspects["Type"] == ["Armchair"]


def test_draft_owned_required_stripped_does_not_get_source_replacement():
    aspects = {"Type": ["Apothecary Cabinet"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Bathroom Vanity with Sink silver glass",
        required_names=["Type"],
        allowed_values_by_name={"Type": ["Apothecary Cabinet", "Bathroom Vanity"]},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={"Type": ["Apothecary Cabinet"]},
        expected_values_by_name={"Type": "Bathroom Vanity"},
    )
    assert "unsourced_required_aspect:Type" in blockers
    assert "Type" not in aspects


def test_draft_vs_gate_conflict_blocks_number_of_pieces_and_assembly():
    aspects = {
        "Number of Pieces": ["7"],
        "Assembly Required": ["No"],
        "Type": ["Dining Set"],
    }
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=(
            "5 Piece Dining Table Set with 4 Chairs. Assembly Required: Yes."
        ),
        required_names=[],
        draft_aspects={
            "Number of Pieces": ["7"],
            "Assembly Required": ["No"],
            "Type": ["Dining Set"],
        },
        expected_values_by_name={
            "Number of Pieces": "5",
            "Assembly Required": "Yes",
            "Type": "Dining Set",
        },
    )
    assert "aspect_draft_conflicts_gate:Number of Pieces" in blockers
    assert "aspect_draft_conflicts_gate:Assembly Required" in blockers
    assert "Number of Pieces" not in aspects
    assert "Assembly Required" not in aspects
    # Type is descriptive and source-supported — not a silent gate swap target.
    assert aspects.get("Type") == ["Dining Set"]


def test_unsourced_optional_draft_keeps_sourced_upstream_color():
    logs = []
    aspects = {"Color": ["Black"], "Type": ["Cabinet"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Black wood storage cabinet",
        required_names=[],
        draft_aspects={"Color": ["Walnut"], "Type": ["Cabinet"]},
        log=logs.append,
    )
    assert blockers == []
    assert aspects["Color"] == ["Black"]
    assert any("Walnut" in line and "ASPECT_DRAFT_SKIP" in line for line in logs)


def test_unsourced_type_draft_does_not_drop_sourced_upstream_accent_chair():
    aspects = {"Type": ["Accent Chair"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=_TYPE_SOURCE,
        required_names=["Type"],
        allowed_values_by_name={"Type": list(_TYPE_ALLOWED) + ["Apothecary Cabinet"]},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={"Type": ["Apothecary Cabinet"]},
        expected_values_by_name={"Type": "Armchair"},
    )
    assert blockers == []
    assert aspects["Type"] == ["Accent Chair"]


def test_multi_value_selection_only_keeps_all_allowed_values():
    aspects = {"Style": ["Modern"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Modern Contemporary living room sofa",
        required_names=[],
        allowed_values_by_name={
            "Style": ["Modern", "Contemporary", "Traditional"],
        },
        aspect_modes_by_name={"Style": "SELECTION_ONLY"},
        draft_aspects={"Style": ["Modern", "Contemporary"]},
    )
    assert blockers == []
    assert aspects["Style"] == ["Modern", "Contemporary"]


def test_multi_value_selection_only_blocks_any_invalid_value():
    aspects = {"Style": ["Modern"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Modern living room sofa",
        required_names=[],
        allowed_values_by_name={"Style": ["Modern", "Contemporary"]},
        aspect_modes_by_name={"Style": "SELECTION_ONLY"},
        draft_aspects={"Style": ["Modern", "Garage"]},
    )
    assert blockers == ["aspect_value_not_allowed:Style=Garage"]
    # Upstream Modern kept — invalid draft must not wipe it.
    assert aspects["Style"] == ["Modern"]


def test_merge_aspects_for_draft_persist_compares_whole_lists():
    merged = merge_aspects_for_draft_persist(
        {"Style": [" modern ", "Contemporary"], "Brand": ["AquaVerve"]},
        {
            "Style": ["Modern", "Contemporary"],
            "Brand": ["AquaVerve"],
            "Color": ["Black"],
        },
    )
    assert merged["Style"] == ["Modern", "Contemporary"]
    assert merged["Color"] == ["Black"]


def test_merge_aspects_persists_full_list_not_truncated_first_value():
    merged = merge_aspects_for_draft_persist(
        {"Room": ["Living Room", "Bedroom"]},
        {"Room": ["Living Room"]},
    )
    # Draft whole list wins over a truncated completed form.
    assert merged["Room"] == ["Living Room", "Bedroom"]


def test_aspect_key_normalization_avoids_duplicate_type_keys():
    aspects = {"type": ["Armchair"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=_TYPE_SOURCE,
        required_names=["Type"],
        allowed_values_by_name={"Type": _TYPE_ALLOWED},
        aspect_modes_by_name={"Type": "SELECTION_ONLY"},
        draft_aspects={"Type": ["Accent Chair"]},
        expected_values_by_name={"Type": "Armchair"},
    )
    assert blockers == []
    # Single logical Type remains (canonical key from upstream spelling).
    type_keys = [key for key in aspects if key.casefold().strip() == "type"]
    assert len(type_keys) == 1
    assert aspects[type_keys[0]] == ["Accent Chair"]


def test_motors_fitment_type_unsourced_draft_keeps_upstream_universal():
    aspects = {
        "Fitment Type": ["Universal"],
        "Type": ["Cabin Air Filter"],
    }
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Cabin Air Filter universal fit for most vehicles",
        required_names=[],
        draft_aspects={"Fitment Type": ["Bogus Fitment"]},
    )
    assert blockers == []
    assert aspects["Fitment Type"] == ["Universal"]
    assert aspects["Type"] == ["Cabin Air Filter"]


def test_merge_aspects_for_draft_persist_keeps_draft_and_writes_empty_autofill():
    original = {"Type": ["Accent Chair"], "Brand": ["AquaVerve"]}
    completed = {
        "Type": ["Armchair"],
        "Brand": ["AquaVerve"],
        "Color": ["Black"],
        "Material": ["Wood"],
    }
    merged = merge_aspects_for_draft_persist(original, completed)
    assert merged["Type"] == ["Accent Chair"]
    assert merged["Brand"] == ["AquaVerve"]
    assert merged["Color"] == ["Black"]
    assert merged["Material"] == ["Wood"]


def test_merge_aspects_for_draft_persist_keeps_normalized_draft_form():
    merged = merge_aspects_for_draft_persist(
        {"Type": ["  accent chair "]},
        {"Type": ["Accent Chair"], "Room": ["Living Room"]},
    )
    assert merged["Type"] == ["Accent Chair"]
    assert merged["Room"] == ["Living Room"]


def test_unseeded_invalid_selection_only_draft_is_not_silently_dropped():
    """Illegal draft values must block even when seed declines and aspects stay empty."""
    aspects = {}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="x",
        draft_aspects={"Features": ["Foo", "Armless"]},
        allowed_values_by_name={
            "Features": ["Armless", "Stackable", "Swivel"],
        },
        aspect_modes_by_name={"Features": "SELECTION_ONLY"},
    )
    assert blockers == ["aspect_value_not_allowed:Features=Foo"]
    assert aspects == {}


def test_sourced_but_not_allowed_draft_features_still_blocks():
    aspects = {}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="armless foldable chair",
        draft_aspects={"Features": ["Armless", "Foldable"]},
        allowed_values_by_name={
            "Features": ["Armless", "Stackable", "Swivel"],
        },
        aspect_modes_by_name={"Features": "SELECTION_ONLY"},
    )
    assert blockers == ["aspect_value_not_allowed:Features=Foldable"]
    assert aspects == {}


def test_required_invalid_selection_only_draft_uses_not_allowed_not_unsourced():
    aspects = {}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="x",
        required_names=["Features"],
        draft_aspects={"Features": ["Foo"]},
        allowed_values_by_name={
            "Features": ["Armless", "Stackable", "Swivel"],
        },
        aspect_modes_by_name={"Features": "SELECTION_ONLY"},
    )
    assert blockers == ["aspect_value_not_allowed:Features=Foo"]
    assert all(not item.startswith("unsourced_required_aspect:") for item in blockers)
    assert aspects == {}


def test_working_selection_only_aspects_without_draft_block_illegal_values():
    aspects = {"Features": ["Armless", "Foldable"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="armless foldable chair",
        allowed_values_by_name={
            "Features": ["Armless", "Stackable", "Swivel"],
        },
        aspect_modes_by_name={"Features": "SELECTION_ONLY"},
    )
    assert blockers == ["aspect_value_not_allowed:Features=Foldable"]
    assert "Features" not in aspects


def test_merge_persist_uses_canonical_ebay_key_spelling():
    merged = merge_aspects_for_draft_persist(
        {" color": ["Black"]},
        {"Color": ["Black"], "Room": ["Living Room"]},
    )
    assert "Color" in merged
    assert " color" not in merged
    assert merged["Color"] == ["Black"]
    assert merged["Room"] == ["Living Room"]
    color_keys = [
        key for key in merged
        if key.casefold().replace(" ", "") == "color"
    ]
    assert color_keys == ["Color"]


def test_material_marble_still_stripped_when_source_lacks_it():
    aspects = {"Material": ["Marble"], "Type": ["Storage Ottoman"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Upholstered storage ottoman with lid",
        expected_values_by_name={"Type": "Storage Ottoman"},
    )
    assert blockers == []
    assert "Material" not in aspects
    assert aspects["Type"] == ["Storage Ottoman"]


def test_quality_gate_expected_aspects_for_storage_ottoman():
    from src.utils.listing_quality_gate import quality_gate_expected_aspects

    expected = quality_gate_expected_aspects(
        title="Storage Ottoman with Lid",
        source_title="Storage Ottoman with Lid",
        source_description="Upholstered storage ottoman. Assembly Required: Yes.",
        attributes={"Assembly Required": "Yes"},
        category_id="20490",
        current_aspects={"Type": ["Storage Ottoman"], "Assembly Required": ["Yes"]},
    )
    assert expected["Type"] == "Storage Ottoman"
    assert expected["Assembly Required"] == "Yes"


def test_guard_and_gate_share_listing_title_dining_set_count():
    from src.utils.listing_quality_gate import (
        expected_dining_set_item_count,
        quality_gate_expected_aspects,
        validate_listing_quality,
    )

    listing_title = "5 Piece Dining Table Set with 4 Chairs"
    source_title = "Set of 8 Dining Table and Chairs"
    category_id = "107578"

    shared = expected_dining_set_item_count(listing_title, category_id=category_id)
    assert shared == "5"

    expected = quality_gate_expected_aspects(
        title=listing_title,
        source_title=source_title,
        category_id=category_id,
    )
    assert expected["Number of Items in Set"] == shared
    assert expected["Number of Pieces"] == shared

    issues = validate_listing_quality(
        {
            "title": listing_title,
            "description": "<p>Five-piece dining set.</p>",
            "categoryId": category_id,
            "aspects": {
                "Type": ["Dining Set"],
                "Number of Items in Set": ["8"],
                "Set Includes": expected.get("Set Includes", "Dining Table & Chairs"),
            },
        },
        source_title=source_title,
        images=["https://example.com/a.jpg", "https://example.com/b.jpg"],
    )
    count_issues = [issue for issue in issues if issue.code == "wrong_set_count"]
    assert count_issues
    assert "5" in count_issues[0].message


def test_wood_does_not_match_hollywood():
    assert value_supported_by_source("Wood", "Hollywood vanity mirror") is False


def test_red_does_not_match_covered():
    assert value_supported_by_source("Red", "covered patio set") is False


def test_wood_matches_solid_wood():
    assert value_supported_by_source("Wood", "solid wood frame") is True


_KITCHEN_ISLAND_MATERIAL_ALLOWED = [
    "Bronze",
    "MDF/Chipboard",
    "Solid Wood",
    "Metal",
    "Wood",
    "Marble",
    "Brass",
    "Ash",
    "Oak",
    "Gold",
    "Black",
    "Rubberwood",
    "Eucalyptus",
    "Acacia",
]


def test_material_aspect_names_are_whole_name_or_suffix_only():
    assert is_material_aspect("Material") is True
    assert is_material_aspect("Countertop Material") is True
    assert is_material_aspect("Base Material") is True
    assert is_material_aspect("Upholstery Fabric") is True
    assert is_material_aspect("Color") is False
    assert is_material_aspect("Type") is False
    assert is_material_aspect("Immaterial") is False
    assert is_material_aspect("Material Safety") is False


def test_material_whole_value_equality_not_substring():
    allowed = list(_KITCHEN_ISLAND_MATERIAL_ALLOWED)
    assert find_source_matching_value(
        "Material",
        "material: Brassiere hardware on an Ashley Oakland cabinet",
        allowed,
    ) is None
    assert find_source_matching_value("Material", "material: Brass", allowed) == "Brass"
    assert find_source_matching_value("Material", "material: Ash", allowed) == "Ash"
    assert find_source_matching_value("Material", "material: Oak", allowed) == "Oak"


def test_material_normalizes_case_whitespace_and_punctuation():
    assert find_source_matching_value(
        "Material",
        "  Material :  MDF / Chipboard  ",
        ["MDF/Chipboard", "Solid Wood"],
    ) == "MDF/Chipboard"
    assert find_source_matching_value(
        "Countertop Material",
        "top: solid-wood",
        ["Solid Wood", "Metal"],
    ) == "Solid Wood"


def test_hardware_finish_color_context_never_fills_material():
    allowed = list(_KITCHEN_ISLAND_MATERIAL_ALLOWED)
    sources = (
        "cabinet with antique bronze pull handles",
        "gold-toned hardware on the drawers",
        "brass knobs and hinges",
        "black finish on the drawers",
    )
    for source in sources:
        for name in ("Material", "Countertop Material", "Base Material"):
            assert find_source_matching_value(name, source, allowed) is None


def test_multiple_materials_bind_to_their_own_part():
    source = "MDF top + metal legs. Kitchen island cart."
    allowed = ["MDF/Chipboard", "Metal", "Solid Wood", "Bronze"]
    assert find_source_matching_value("Countertop Material", source, allowed) == "MDF/Chipboard"
    assert find_source_matching_value("Base Material", source, allowed) is None
    assert find_source_matching_value("Material", source, allowed) is None


def test_negated_material_is_not_used():
    allowed = ["MDF/Chipboard", "Solid Wood", "Metal"]
    assert find_source_matching_value("Material", "no MDF, metal frame", allowed) == "Metal"
    assert find_source_matching_value("Material", "not solid wood. Plastic body.", allowed) is None
    assert find_source_matching_value(
        "Material",
        "cabinet with no MDF",
        ["MDF/Chipboard"],
    ) is None


def test_empty_or_none_source_leaves_material_unset():
    allowed = ["MDF/Chipboard", "Solid Wood"]
    assert find_source_matching_value("Material", "", allowed) is None
    assert find_source_matching_value("Material", None, allowed) is None


def test_allowed_list_missing_mapped_value_does_not_invent():
    assert find_source_matching_value(
        "Material",
        "material: MDF",
        ["Solid Wood", "Metal"],
    ) is None
    assert find_source_matching_value(
        "Material",
        "material: rubberwood",
        ["Metal", "MDF/Chipboard"],
    ) is None


def test_mdf_family_maps_only_to_exact_allowed_chipboard_value():
    assert find_source_matching_value(
        "Material",
        "material: particle board",
        ["MDF/Chipboard", "Solid Wood"],
    ) == "MDF/Chipboard"
    assert find_source_matching_value(
        "Material",
        "medium density fiberboard cabinet",
        ["MDF/Chipboard", "Solid Wood"],
    ) == "MDF/Chipboard"
    assert find_source_matching_value(
        "Material",
        "material: MDF",
        ["Solid Wood"],
    ) is None


def test_rubberwood_family_maps_to_own_value_or_solid_wood():
    assert find_source_matching_value(
        "Material",
        "material: Rubberwood",
        ["Rubberwood", "Solid Wood"],
    ) == "Rubberwood"
    assert find_source_matching_value(
        "Material",
        "material: eucalyptus",
        ["Solid Wood", "Metal"],
    ) == "Solid Wood"
    assert find_source_matching_value(
        "Material",
        "material: acacia",
        ["Acacia", "MDF/Chipboard"],
    ) == "Acacia"
    assert find_source_matching_value(
        "Material",
        "material: MDF",
        ["Solid Wood", "MDF/Chipboard"],
    ) == "MDF/Chipboard"


def test_non_material_aspects_still_use_first_source_hit():
    source = "Silver glass wall mirror with antique bronze pull handles"
    assert find_source_matching_value("Color", source, ["Red", "Silver", "Black", "Bronze"]) == "Silver"


def test_n707_mdf_cabinet_with_bronze_handles_does_not_fill_bronze():
    source = build_giga_source_text(
        title="Kitchen Island Cabinet with Antique Bronze Pull Handles",
        description="MDF cabinet with antique bronze pull handles / bronze hardware.",
        attributes={
            "Assembled Length (in.)": "48",
            "Assembled Width (in.)": "18",
            "Assembled Height (in.)": "36",
        },
    )
    aspects = {
        "Type": ["Kitchen Island"],
        "Countertop Material": ["Bronze"],
        "Base Material": ["Bronze"],
        "Material": ["Bronze"],
    }
    allowed = {name: list(_KITCHEN_ISLAND_MATERIAL_ALLOWED) for name in (
        "Countertop Material",
        "Base Material",
        "Material",
    )}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text=source,
        required_names=["Countertop Material", "Base Material", "Material"],
        allowed_values_by_name=allowed,
        sku="N707-SHAPED",
    )

    serialized = str(aspects).lower()
    assert "bronze" not in serialized
    assert "Countertop Material" not in aspects
    assert "Base Material" not in aspects
    assert "unsourced_required_aspect:Countertop Material" in blockers
    assert "unsourced_required_aspect:Base Material" in blockers
    assert aspects.get("Material") == ["MDF/Chipboard"]


def test_missing_required_material_blocks_without_default():
    aspects = {"Type": ["Kitchen Island"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Kitchen island with storage drawers and antique bronze pull handles",
        required_names=["Countertop Material", "Base Material"],
        allowed_values_by_name={
            "Countertop Material": _KITCHEN_ISLAND_MATERIAL_ALLOWED,
            "Base Material": _KITCHEN_ISLAND_MATERIAL_ALLOWED,
        },
    )
    assert "unsourced_required_aspect:Countertop Material" in blockers
    assert "unsourced_required_aspect:Base Material" in blockers
    assert "Countertop Material" not in aspects
    assert "Base Material" not in aspects


def _ok_blacklist(tmp_path):
    path = tmp_path / "ok_high_fee.json"
    path.write_text('{"categoryIds": ["00000"]}', encoding="utf-8")
    return str(path)


def test_insertion_and_listing_promo_covered_real_shape_is_allowed(tmp_path):
    import json
    from pathlib import Path

    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "zero_fee_gate" / "ebay_insertion_listing_promo_covered.json").read_text(encoding="utf-8")
    )
    reason = evaluate_insertion_fee_preview(
        listing_fees=payload,
        lookup_attempted=True,
        blacklist_path=_ok_blacklist(tmp_path),
    )
    assert reason is None


def test_insertion_fee_real_shape_zero_is_allowed(tmp_path):
    reason = evaluate_insertion_fee_preview(
        listing_fees={
            "feeSummaries": [{
                "fees": [{
                    "feeType": "InsertionFee",
                    "amount": {"value": "0.00", "currency": "USD"},
                }]
            }]
        },
        lookup_attempted=True,
        blacklist_path=_ok_blacklist(tmp_path),
    )
    assert reason is None


def test_insertion_fee_real_shape_twenty_blocks(tmp_path):
    reason = evaluate_insertion_fee_preview(
        listing_fees={
            "feeSummaries": [{
                "fees": [{
                    "feeType": "InsertionFee",
                    "amount": {"value": "20.00", "currency": "USD"},
                }]
            }]
        },
        lookup_attempted=True,
        blacklist_path=_ok_blacklist(tmp_path),
    )
    assert reason == "high_insertion_fee"


def test_successful_fee_response_without_insertion_line_is_unavailable(tmp_path):
    assert evaluate_insertion_fee_preview(
        listing_fees={"feeSummaries": [{"fees": []}]},
        lookup_attempted=True,
        blacklist_path=_ok_blacklist(tmp_path),
    ) == "fee_preview_unavailable"


def test_insertion_fee_at_least_20_blocks(tmp_path):
    reason = evaluate_insertion_fee_preview(
        listing_fees={
            "feeSummaries": [{
                "fees": [{
                    "feeType": "INSERTION_FEE",
                    "amount": {"value": "20.00", "currency": "USD"},
                }]
            }]
        },
        lookup_attempted=True,
        blacklist_path=_ok_blacklist(tmp_path),
    )
    assert reason == "high_insertion_fee"


def test_motors_vehicles_trailers_path_blocks_as_high_insertion_fee():
    reason = evaluate_insertion_fee_preview(
        category_id="6001",
        category_path="eBay Motors/Vehicles/Trailers/Cars & Trucks",
        category_name="Cars & Trucks",
        lookup_attempted=False,
    )
    assert reason == "high_insertion_fee"


def test_fee_preview_unavailable_bypass_removed(monkeypatch):
    monkeypatch.delenv("ALLOW_PUBLISH_WITHOUT_FEE_PREVIEW", raising=False)
    assert evaluate_insertion_fee_preview(
        lookup_attempted=True,
        lookup_error=RuntimeError("boom"),
    ) == "fee_preview_unavailable"

    monkeypatch.setenv("ALLOW_PUBLISH_WITHOUT_FEE_PREVIEW", "1")
    assert evaluate_insertion_fee_preview(
        lookup_attempted=True,
        lookup_error=RuntimeError("boom"),
    ) == "fee_preview_unavailable"


def test_high_fee_category_blacklist_json(tmp_path):
    path = tmp_path / "high_fee.json"
    path.write_text('{"categoryIds": ["99999"]}', encoding="utf-8")
    reason = evaluate_insertion_fee_preview(
        category_id="99999",
        blacklist_path=str(path),
        lookup_attempted=False,
    )
    assert reason == "high_insertion_fee"


def test_blacklist_reads_categories_path_entries(tmp_path):
    path = tmp_path / "skip.json"
    path.write_text(
        '{"categories": [{"id": "38204", "path": "Home & Garden > Furniture > Coffee Tables"}]}',
        encoding="utf-8",
    )
    assert load_high_fee_category_blacklist(str(path)) == {"38204"}
    assert load_high_fee_blacklist_category_paths(str(path)) == {
        "38204": "Home & Garden > Furniture > Coffee Tables",
    }
    assert evaluate_insertion_fee_preview(
        category_id="99999",
        category_path="Home & Garden > Furniture > Coffee Tables",
        blacklist_path=str(path),
        lookup_attempted=False,
    ) == "high_insertion_fee"


def test_blacklist_reads_publish_skip_category_ids(tmp_path):
    path = tmp_path / "skip.json"
    path.write_text('{"publish_skip_category_ids": ["1162", "6001"]}', encoding="utf-8")
    assert load_high_fee_category_blacklist(str(path)) == {"1162", "6001"}
    assert evaluate_insertion_fee_preview(
        category_id="1162",
        blacklist_path=str(path),
        lookup_attempted=False,
    ) == "high_insertion_fee"


def test_default_blacklist_path_used_when_env_unset(tmp_path, monkeypatch):
    from src.utils import publish_fee_preview

    data_dir = tmp_path / "data" / "config"
    data_dir.mkdir(parents=True)
    (data_dir / "high_fee_category_blacklist.json").write_text(
        '{"publish_skip_category_ids": ["77777"]}',
        encoding="utf-8",
    )
    monkeypatch.delenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", raising=False)
    monkeypatch.setattr(publish_fee_preview, "REPO_ROOT", tmp_path)
    assert "77777" in load_high_fee_category_blacklist()


def test_empty_blacklist_logs_warning(tmp_path, monkeypatch, caplog):
    import logging
    from src.utils import publish_fee_preview

    monkeypatch.delenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", raising=False)
    monkeypatch.setattr(publish_fee_preview, "REPO_ROOT", tmp_path)
    publish_fee_preview._EMPTY_BLACKLIST_WARNED.clear()
    with caplog.at_level(logging.WARNING):
        assert load_high_fee_category_blacklist() == set()
    assert "empty" in caplog.text.lower()


def test_trailer_hitches_leaf_does_not_block(tmp_path):
    assert evaluate_insertion_fee_preview(
        category_id="33653",
        category_name="Trailer Hitches",
        category_path="eBay Motors / Parts & Accessories / Trailer Hitches",
        lookup_attempted=False,
        blacklist_path=_ok_blacklist(tmp_path),
    ) is None


def test_real_ebay_other_vehicles_and_trailers_path_blocks():
    assert evaluate_insertion_fee_preview(
        category_id="6038",
        category_path="eBay Motors > Other Vehicles & Trailers > Trailers",
        category_name="Trailers",
        lookup_attempted=False,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    ) == "high_insertion_fee"


def test_full_taxonomy_cars_and_trucks_path_blocks():
    assert evaluate_insertion_fee_preview(
        category_id="6001",
        category_path="eBay Motors / Vehicles / Trailers / Cars & Trucks",
        category_name="Cars & Trucks",
        lookup_attempted=False,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    ) == "high_insertion_fee"


def _long_description(text: str) -> str:
    return (
        f"<div><h3>KEY FEATURES</h3><ul>"
        f"<li>{text}</li>"
        f"<li>Sturdy frame supports everyday household use.</li>"
        f"<li>Compact profile fits apartments and family rooms.</li>"
        f"</ul></div>"
    )


def _product(**overrides):
    product = {
        "sku": "W3204P565677",
        "title": "Coffee Table",
        "description": "Coffee table with storage shelf. Assembly Required Yes.",
        "price": 50.0,
        "suggested_price": 99.0,
        "shipping": 0,
        "images": ["https://example.com/a.jpg"],
        "attributes": {
            "Assembled Length (in.)": "40",
            "Assembled Width (in.)": "20",
            "Assembled Height (in.)": "18",
        },
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 40},
        "optimization": {
            "title": "Coffee Table",
            "description": _long_description("Storage shelf keeps living room essentials organized."),
            "categoryId": "38204",
            "categoryName": "Coffee Tables",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Coffee Table"],
                "Assembly Required": ["Yes"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        },
    }
    product.update(overrides)
    return product


class _FakeOAuth:
    def __init__(self, environment):
        self.environment = environment

    def is_authorized(self):
        return True

    def get_application_token(self):
        return "app-token"

    def get_valid_token(self):
        return "token"


class _FakeCategoryMatcher:
    def __init__(
        self,
        oauth,
        *,
        required=None,
        returned_aspects=None,
        category_id="38204",
        category_name="Coffee Tables",
    ):
        self._required = required or []
        self._returned_aspects = returned_aspects
        self._category_id = category_id
        self._category_name = category_name

    def canonicalize_category(self, title_context, category_id, category_name, description=None):
        return category_id, category_name

    def get_category_and_aspects(self, title, aspects, description):
        completed = dict(self._returned_aspects) if self._returned_aspects is not None else dict(aspects)
        return self._category_id, self._category_name, completed

    def is_category_plausible_for_text(self, title, category_id, category_name):
        return True

    def _get_category_aspects(self, category_id):
        return list(self._required), []


class _FakePolicyManager:
    def __init__(self, oauth):
        pass


def _stub_publish_common(monkeypatch, *, matcher_factory=None, qc_blockers=None):
    monkeypatch.setattr(batch_publish, "enrich_product_dimensions", lambda product: None)
    monkeypatch.setattr(batch_publish, "validate_product", lambda product: [])
    monkeypatch.setattr(batch_publish, "fetch_market_price", lambda title: None)
    monkeypatch.setattr(batch_publish, "calculate_smart_final_price", lambda product, market_p: 99.0)
    monkeypatch.setattr(batch_publish, "record_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "_is_sellable_leaf_category", lambda oauth, category_id: True)
    monkeypatch.setattr(
        batch_publish,
        "run_listing_qc",
        lambda **kwargs: {
            "status": "pass",
            "blockers": list(qc_blockers or []),
            "warnings": [],
            "source_fingerprint": "source",
            "candidate_fingerprint": "candidate",
            "ruleset_version": "listing-qc-v2",
            "fact_sheet_version": 4,
        },
    )
    # Accent-chair / sofa category rows need a green prepublish verdict so
    # draft-aspect assertions are not masked by leaf_family_conflict.
    monkeypatch.setattr(
        "src.utils.category_decision.prepublish_category_check",
        lambda *args, **kwargs: type("V", (), {"ok": True, "reason": None, "suggestion": None})(),
    )
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _FakeOAuth)
    monkeypatch.setattr(
        "src.services.ebay_category_matcher.EbayCategoryMatcher",
        matcher_factory or (lambda oauth: _FakeCategoryMatcher(oauth)),
    )
    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", _FakePolicyManager)


def test_publish_dry_run_does_not_persist_template_aspects(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        batch_publish,
        "_persist_prepared_listing",
        lambda **kwargs: persisted.append(kwargs),
    )
    _stub_publish_common(
        monkeypatch,
        matcher_factory=lambda oauth: _FakeCategoryMatcher(
            oauth,
            returned_aspects={
                "Brand": ["AquaVerve"],
                "Type": ["Apothecary Cabinet"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        ),
    )

    result = batch_publish.publish_single_product(_product(), dry_run=True)

    assert result["status"] == "dry_run"
    assert persisted == []


def test_publish_blocks_unsourced_required_aspect(monkeypatch):
    _stub_publish_common(
        monkeypatch,
        matcher_factory=lambda oauth: _FakeCategoryMatcher(
            oauth,
            required=[{"name": "Type", "required": True, "allowed_values": ["Apothecary Cabinet"]}],
            returned_aspects={
                "Brand": ["AquaVerve"],
                "Type": ["Apothecary Cabinet"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        ),
    )
    product = _product(
        title="Plain household widget",
        description="Plain household widget with no furniture type mentioned.",
        optimization={
            "title": "Plain household widget",
            "description": _long_description("Plain household widget with no furniture type mentioned."),
            "categoryId": "38204",
            "categoryName": "Coffee Tables",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Apothecary Cabinet"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        },
    )

    result = batch_publish.publish_single_product(product, dry_run=True)

    assert result["status"] == "error"
    assert "unsourced_required_aspect:Type" in result["message"]
    assert result["copy_skip_reasons"] == ["unsourced_required_aspect:Type"]


def test_publish_keeps_sourced_type_when_template_tries_to_overwrite(monkeypatch):
    captured = {}
    real_guard = batch_publish.guard_unsourced_aspects

    def _capture(aspects, **kwargs):
        captured["aspects"] = dict(aspects)
        return real_guard(aspects, **kwargs)

    monkeypatch.setattr(batch_publish, "guard_unsourced_aspects", _capture)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    _stub_publish_common(
        monkeypatch,
        matcher_factory=lambda oauth: _FakeCategoryMatcher(
            oauth,
            category_id="32878",
            category_name="Bathroom Vanities",
            required=[{"name": "Type", "required": True, "allowed_values": ["Apothecary Cabinet", "Bathroom Vanity"]}],
            returned_aspects={
                "Brand": ["AquaVerve"],
                "Type": ["Apothecary Cabinet"],
                "Item Length": ["24 in"],
                "Item Width": ["18 in"],
                "Item Height": ["32 in"],
            },
        ),
    )
    product = _product(
        title="Bathroom Vanity with Sink",
        description="Bathroom vanity cabinet with silver glass doors.",
        attributes={
            "Assembled Length (in.)": "24",
            "Assembled Width (in.)": "18",
            "Assembled Height (in.)": "32",
            "Type": "Bathroom Vanity",
        },
        optimization={
            "title": "Bathroom Vanity with Sink",
            "description": _long_description("Bathroom vanity cabinet with silver glass doors."),
            "categoryId": "32878",
            "categoryName": "Bathroom Vanities",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Bathroom Vanity"],
                "Item Length": ["24 in"],
                "Item Width": ["18 in"],
                "Item Height": ["32 in"],
            },
        },
    )

    result = batch_publish.publish_single_product(product, dry_run=True)

    assert result["status"] == "dry_run"
    assert captured["aspects"]["Type"] == ["Bathroom Vanity"]


def _accent_chair_product(**overrides):
    product = _product(
        sku="W5568P534216",
        title="Modern Accent Chair Metal Frame Accent Chair",
        description="Modern Accent Chair. Product Type: Accent Chair. Metal frame.",
        attributes={
            "Assembled Length (in.)": "28",
            "Assembled Width (in.)": "30",
            "Assembled Height (in.)": "32",
            "Type": "Accent Chair",
        },
        optimization={
            "title": "Modern Accent Chair Metal Frame Living Room Chair",
            "description": _long_description(
                "Modern Accent Chair with metal frame. Product Type: Accent Chair."
            ),
            "categoryId": "38208",
            "categoryName": "Sofas, Armchairs & Couches",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Accent Chair"],
                "Item Length": ["28 in"],
                "Item Width": ["30 in"],
                "Item Height": ["32 in"],
            },
        },
    )
    product.update(overrides)
    return product


def _accent_chair_matcher(oauth, *, draft_type="Accent Chair", mode="SELECTION_ONLY"):
    return _FakeCategoryMatcher(
        oauth,
        category_id="38208",
        category_name="Sofas, Armchairs & Couches",
        required=[{
            "name": "Type",
            "required": True,
            "mode": mode,
            "allowed_values": list(_TYPE_ALLOWED),
        }],
        returned_aspects={
            "Brand": ["AquaVerve"],
            "Type": ["Armchair"],
            "Item Length": ["28 in"],
            "Item Width": ["30 in"],
            "Item Height": ["32 in"],
        },
    )


def test_publish_dry_run_keeps_draft_accent_chair_over_inferred_armchair(monkeypatch):
    captured = {}
    real_guard = batch_publish.guard_unsourced_aspects

    def _capture(aspects, **kwargs):
        result = real_guard(aspects, **kwargs)
        captured["aspects"] = dict(aspects)
        captured["draft_aspects"] = dict(kwargs.get("draft_aspects") or {})
        captured["modes"] = dict(kwargs.get("aspect_modes_by_name") or {})
        return result

    monkeypatch.setattr(batch_publish, "guard_unsourced_aspects", _capture)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    _stub_publish_common(monkeypatch, matcher_factory=_accent_chair_matcher)

    result = batch_publish.publish_single_product(_accent_chair_product(), dry_run=True)

    assert result["status"] == "dry_run"
    assert captured["draft_aspects"]["Type"] == ["Accent Chair"]
    assert captured["modes"]["Type"] == "SELECTION_ONLY"
    assert captured["aspects"]["Type"] == ["Accent Chair"]


def test_publish_blocks_selection_only_draft_prefix_not_nearest_guess(monkeypatch):
    _stub_publish_common(monkeypatch, matcher_factory=_accent_chair_matcher)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    product = _accent_chair_product()
    product["optimization"] = dict(product["optimization"])
    product["optimization"]["aspects"] = dict(product["optimization"]["aspects"])
    product["optimization"]["aspects"]["Type"] = ["Accent"]

    result = batch_publish.publish_single_product(product, dry_run=True)

    assert result["status"] == "error"
    assert "aspect_value_not_allowed:Type=Accent" in result["message"]
    assert result["copy_skip_reasons"] == ["aspect_value_not_allowed:Type=Accent"]


def test_live_publish_persists_draft_type_not_inferred_armchair(monkeypatch):
    persisted = []
    published = []

    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            self.sent_aspects = None

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            self.sent_aspects = dict((product or {}).get("aspects") or {})
            return {"status": "success"}

        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            return {"offerId": "offer-accent"}

        def get_offer(self, offer_id):
            return {"status": "UNPUBLISHED"}

        def get_listing_fees(self, offer_id):
            return {
                "feeSummaries": [{
                    "fees": [{
                        "feeType": "InsertionFee",
                        "amount": {"value": "0.00", "currency": "USD"},
                    }]
                }]
            }

        def get_inventory_item(self, sku):
            return {"product": {}}

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "366783187245"}

    clients = []

    def _client_factory(oauth, policy_manager):
        client = _FakeEbayClient(oauth, policy_manager)
        clients.append(client)
        return client

    monkeypatch.setattr(
        batch_publish,
        "_persist_prepared_listing",
        lambda **kwargs: persisted.append(kwargs),
    )
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda *args, **kwargs: (None, "ok"))
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr(batch_publish, "update_product_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "batch_publish.verify_publish_readback",
        lambda *a, **k: {"status": "ok", "passed": True, "issues": [], "transport_failures": []},
    )
    monkeypatch.setattr("batch_publish.build_publish_readback_expectation", lambda **k: {})
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _client_factory)
    _stub_publish_common(monkeypatch, matcher_factory=_accent_chair_matcher)

    result = batch_publish.publish_single_product(_accent_chair_product(), dry_run=False)

    assert result["status"] == "success"
    assert published == ["offer-accent"]
    assert clients[0].sent_aspects["Type"] == ["Accent Chair"]
    assert persisted
    assert persisted[0]["aspects"]["Type"] == ["Accent Chair"]
    # Empty draft Color may be auto-filled later; Type must never become Armchair.
    assert persisted[0]["aspects"]["Type"] != ["Armchair"]


def test_publish_blocks_when_insertion_fee_is_at_least_20(monkeypatch):
    published = []
    deleted = []

    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            self.published = False

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            return {"status": "success"}

        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            return {"status": "DRAFT"}

        def get_listing_fees(self, offer_id):
            return {
                "feeSummaries": [{
                    "fees": [{
                        "feeType": "InsertionFee",
                        "amount": {"value": "20.00", "currency": "USD"},
                    }]
                }]
            }

        def delete_offer(self, offer_id):
            deleted.append(offer_id)
            return True

        def publish_offer(self, offer_id):
            published.append(offer_id)
            self.published = True
            return {"listingId": "listing-1"}

    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda *args, **kwargs: (None, "ok"))
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _FakeEbayClient)
    _stub_publish_common(monkeypatch)

    result = batch_publish.publish_single_product(_product(), dry_run=False)

    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == ["high_insertion_fee"]
    assert published == []
    assert deleted == ["offer-1"]


def _product_with_confident_material(**overrides):
    product = _product(
        title="Kitchen Island with MDF Countertop",
        description="material: MDF. MDF countertop and storage drawers.",
        attributes={
            "Assembled Length (in.)": "48",
            "Assembled Width (in.)": "18",
            "Assembled Height (in.)": "36",
            "Material": "MDF",
            "Top Material": "MDF",
        },
        optimization={
            "title": "Kitchen Island with MDF Countertop",
            "description": _long_description("material: MDF. MDF countertop and storage drawers."),
            "categoryId": "260157",
            "categoryName": "Kitchen Islands",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Kitchen Island"],
                "Item Length": ["48 in"],
                "Item Width": ["18 in"],
                "Item Height": ["36 in"],
            },
        },
    )
    product.update(overrides)
    return product


def _material_matcher(oauth):
    return _FakeCategoryMatcher(
        oauth,
        category_id="260157",
        category_name="Kitchen Islands",
        required=[
            {
                "name": "Material",
                "required": True,
                "allowed_values": ["MDF/Chipboard", "Solid Wood", "Bronze"],
            },
            {
                "name": "Countertop Material",
                "required": True,
                "allowed_values": ["MDF/Chipboard", "Solid Wood", "Bronze"],
            },
        ],
        returned_aspects={
            "Brand": ["AquaVerve"],
            "Type": ["Kitchen Island"],
            "Item Length": ["48 in"],
            "Item Width": ["18 in"],
            "Item Height": ["36 in"],
        },
    )


def test_fee_block_does_not_persist_autofilled_aspects(monkeypatch):
    persisted = []
    published = []
    deleted = []

    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            return {"status": "success"}

        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            return {"status": "UNPUBLISHED"}

        def get_listing_fees(self, offer_id):
            return {
                "feeSummaries": [{
                    "fees": [{
                        "feeType": "InsertionFee",
                        "amount": {"value": "20.00", "currency": "USD"},
                    }]
                }]
            }

        def delete_offer(self, offer_id):
            deleted.append(offer_id)
            return True

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "listing-1"}

    monkeypatch.setattr(
        batch_publish,
        "_persist_prepared_listing",
        lambda **kwargs: persisted.append(kwargs),
    )
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda *args, **kwargs: (None, "ok"))
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _FakeEbayClient)
    _stub_publish_common(monkeypatch, matcher_factory=_material_matcher)

    result = batch_publish.publish_single_product(_product_with_confident_material(), dry_run=False)

    assert result["status"] == "error"
    assert result.get("blocked_fee") is True
    assert published == []
    assert deleted == ["offer-1"]
    assert persisted == []


def test_successful_publish_persists_after_fee_gate(monkeypatch):
    persisted = []
    published = []

    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            return {"status": "success"}

        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            return {"status": "UNPUBLISHED"}

        def get_listing_fees(self, offer_id):
            return {
                "feeSummaries": [{
                    "fees": [{
                        "feeType": "InsertionFee",
                        "amount": {"value": "0.00", "currency": "USD"},
                    }]
                }]
            }

        def get_inventory_item(self, sku):
            return {"product": {}}

        def publish_offer(self, offer_id):
            published.append(offer_id)
            return {"listingId": "listing-1"}

    monkeypatch.setattr(
        batch_publish,
        "_persist_prepared_listing",
        lambda **kwargs: persisted.append(kwargs),
    )
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda *args, **kwargs: (None, "ok"))
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr(batch_publish, "update_product_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "batch_publish.verify_publish_readback",
        lambda *a, **k: {"status": "ok", "passed": True, "issues": [], "transport_failures": []},
    )
    monkeypatch.setattr("batch_publish.build_publish_readback_expectation", lambda **k: {})
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _FakeEbayClient)
    _stub_publish_common(monkeypatch, matcher_factory=_material_matcher)

    result = batch_publish.publish_single_product(_product_with_confident_material(), dry_run=False)

    assert result["status"] == "success"
    assert persisted
    aspects = persisted[0]["aspects"]
    assert aspects.get("Material") == ["MDF/Chipboard"]
    assert aspects.get("Countertop Material") == ["MDF/Chipboard"]
    assert "Bronze" not in str(aspects)


def test_publish_blocks_motors_trailers_using_full_category_path(monkeypatch):
    class _Matcher(_FakeCategoryMatcher):
        def get_category_path(self, category_id, category_name="", title=""):
            return "eBay Motors / Vehicles / Trailers / Utility Trailers"

    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _Matcher(oauth))
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")

    result = batch_publish.publish_single_product(_product(), dry_run=True)

    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == ["high_insertion_fee"]


_MOTORS_TRAILER_TREE = {
    "categoryTreeId": "100",
    "categoryTreeVersion": "1",
    "rootCategoryNode": {
        "category": {"categoryId": "6000", "categoryName": "eBay Motors"},
        "childCategoryTreeNodes": [
            {
                "category": {
                    "categoryId": "66468",
                    "categoryName": "Other Vehicles & Trailers",
                },
                "childCategoryTreeNodes": [
                    {"category": {"categoryId": "6038", "categoryName": "Trailers"}},
                ],
            }
        ],
    },
}


def _forbid_taxonomy_tree_http(monkeypatch):
    calls = []

    def _track(url, *args, **kwargs):
        text = str(url)
        calls.append(text)
        if "get_category_subtree" in text or (
            "/category_tree/" in text
            and "get_item_aspects_for_category" not in text
            and "get_category_suggestions" not in text
        ):
            raise AssertionError(f"publish/dry-run must not fetch taxonomy trees: {text}")

        class _Resp:
            status_code = 500

            def json(self):
                return {}

        return _Resp()

    monkeypatch.setattr("requests.get", _track)
    monkeypatch.setattr("src.services.ebay_category_matcher.requests.get", _track)
    monkeypatch.setattr("batch_publish.requests.get", _track)
    return calls


def _cache_backed_matcher(monkeypatch, tmp_path, trees=None):
    from src.services.ebay_category_matcher import EbayCategoryMatcher
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)
    for tree_id, payload in (trees or {"100": _MOTORS_TRAILER_TREE}).items():
        category_tree_cache.write_category_tree_cache(tree_id, payload, cache_dir)

    class _Matcher(_FakeCategoryMatcher):
        def __init__(self, oauth, **kwargs):
            super().__init__(oauth, **kwargs)

        def get_category_path(self, category_id, category_name="", title=""):
            return EbayCategoryMatcher(_FakeOAuth("PRODUCTION")).get_category_path(
                category_id,
                category_name=category_name,
                title=title,
            )

    return _Matcher, cache_dir


def test_publish_dry_run_builds_path_from_cache_without_tree_http(monkeypatch, tmp_path):
    matcher_cls, _ = _cache_backed_matcher(monkeypatch, tmp_path)
    _forbid_taxonomy_tree_http(monkeypatch)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(
        batch_publish,
        "_normalize_generated_listing",
        lambda listing, **kwargs: listing,
    )
    _stub_publish_common(
        monkeypatch,
        matcher_factory=lambda oauth: matcher_cls(
            oauth, category_id="6038", category_name="Trailers"
        ),
    )
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")

    product = _product(
        title="Utility Trailer 12ft Single Axle",
        description="Single-axle utility trailer for cargo.",
        optimization={
            "title": "Utility Trailer 12ft Single Axle",
            "description": _long_description("Single-axle utility trailer for cargo."),
            "categoryId": "6038",
            "categoryName": "Trailers",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Utility Trailer"],
                "Item Length": ["12 ft"],
                "Item Width": ["6 ft"],
                "Item Height": ["3 ft"],
            },
        }
    )
    path = batch_publish.resolve_publish_category_path(
        matcher_cls(_FakeOAuth("PRODUCTION")),
        "6038",
        category_name="Trailers",
    )
    assert path == "eBay Motors > Other Vehicles & Trailers > Trailers"

    result = batch_publish.publish_single_product(product, dry_run=True)
    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == ["high_insertion_fee"]


def test_missing_cache_returns_empty_and_blacklist_by_id_still_blocks(monkeypatch, tmp_path):
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)
    _forbid_taxonomy_tree_http(monkeypatch)

    blacklist = tmp_path / "high_fee.json"
    blacklist.write_text('{"publish_skip_category_ids": ["88888"]}', encoding="utf-8")
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", str(blacklist))

    from src.services.ebay_category_matcher import EbayCategoryMatcher

    class _Matcher(_FakeCategoryMatcher):
        def __init__(self, oauth):
            super().__init__(oauth, category_id="88888", category_name="Widget")

        def get_category_path(self, category_id, category_name="", title=""):
            return EbayCategoryMatcher(_FakeOAuth("PRODUCTION")).get_category_path(
                category_id,
                category_name=category_name,
                title=title,
            )

    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(
        batch_publish,
        "_normalize_generated_listing",
        lambda listing, **kwargs: listing,
    )
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _Matcher(oauth))

    matcher = _Matcher(_FakeOAuth("PRODUCTION"))
    assert matcher.get_category_path("88888", category_name="Widget") == ""
    assert batch_publish.resolve_publish_category_path(
        matcher,
        "88888",
        category_name="Widget",
        blacklist_path=str(blacklist),
    ) == ""

    product = _product(
        title="Household Widget",
        description="Plain household widget for indoor use.",
        optimization={
            "title": "Household Widget",
            "description": _long_description("Plain household widget for indoor use."),
            "categoryId": "88888",
            "categoryName": "Widget",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Widget"],
                "Item Length": ["10 in"],
                "Item Width": ["8 in"],
                "Item Height": ["6 in"],
            },
        }
    )
    result = batch_publish.publish_single_product(product, dry_run=True)
    assert result["status"] == "error"
    from src.utils.category_decision import HOLD_TREE_UNAVAILABLE_ERROR

    assert HOLD_TREE_UNAVAILABLE_ERROR in str(result.get("message") or "")


def test_resolve_path_uses_blacklist_categories_path_when_cache_missing(monkeypatch, tmp_path):
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)

    blacklist = tmp_path / "high_fee.json"
    blacklist.write_text(
        '{"categories": [{"id": "6038", "path": "eBay Motors > Other Vehicles & Trailers > Trailers"}]}',
        encoding="utf-8",
    )

    from src.services.ebay_category_matcher import EbayCategoryMatcher

    class _Matcher(_FakeCategoryMatcher):
        def get_category_path(self, category_id, category_name="", title=""):
            return EbayCategoryMatcher(_FakeOAuth("PRODUCTION")).get_category_path(
                category_id, category_name=category_name, title=title
            )

    path = batch_publish.resolve_publish_category_path(
        _Matcher(_FakeOAuth("PRODUCTION")),
        "6038",
        category_name="Trailers",
        blacklist_path=str(blacklist),
    )
    assert path == "eBay Motors > Other Vehicles & Trailers > Trailers"
