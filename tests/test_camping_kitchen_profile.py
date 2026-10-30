"""Camping-kitchen profile and matcher guard.

One parametrized case per spec table row (17 real + SYNTHETIC-1..5), plus
word-boundary coverage for ``is_camping_kitchen_title`` and extra SYNTHETIC
edge cases. Synthetic ids stay labelled SYNTHETIC.
"""

from __future__ import annotations

import pytest

from src.services.ebay_category_matcher import EbayCategoryMatcher
from src.utils.listing_quality_gate import classify_listing_profile
from src.utils.product_context_signals import is_camping_kitchen_title


class _DummyOauth:
    def get_application_token(self):
        raise AssertionError("network call should not be needed")


CAMPING_REMOVE = frozenset({"Room", "Upholstery Material", "Upholstery Fabric"})


def _matcher() -> EbayCategoryMatcher:
    return EbayCategoryMatcher(_DummyOauth())


# Titles are the spec fragments, expanded only enough to keep every marker the
# row names. Ellipses in the spec are not part of the title text.
CASES = [
    {
        "sku": "W465P439797",
        "title": (
            "Folding Camping Kitchen Table - Portable Outdoor Cooking Station "
            "with Heat Resistant Top, 4 Side Tables, Storage Rack & Lamp Post "
            "for Picnics & Camping Trips"
        ),
        "stored": "112590",
        "kind": "camping_kitchen_table",
        "category_id": "112590",
        "category_name": "Patio & Garden Tables",
        "type_value": "Camping Table",
        "indoor_outdoor": "Outdoor",
        "remove_aspects": CAMPING_REMOVE,
        "room": None,
        "not_kinds": ("side_table",),
        "not_categories": ("38200", "54235"),
        "hint": ("112590", "Patio & Garden Tables"),
        "plausible": {"112590": True, "54235": False, "38200": False, "16038": True},
    },
    {
        "sku": "W465P439798",
        "title": (
            "Folding Camping Kitchen Table - Portable Outdoor Cooking Station "
            "with Heat Resistant Top, 4 Side Tables, Storage Rack for Picnics "
            "& Camping Trips"
        ),
        "stored": "112590",
        "kind": "camping_kitchen_table",
        "category_id": "112590",
        "category_name": "Patio & Garden Tables",
        "type_value": "Camping Table",
        "indoor_outdoor": "Outdoor",
        "remove_aspects": CAMPING_REMOVE,
        "room": None,
        "not_kinds": ("side_table",),
        "not_categories": ("38200", "54235"),
        "hint": ("112590", "Patio & Garden Tables"),
        "plausible": {"112590": True, "54235": False, "38200": False},
    },
    {
        "sku": "W465P440752",
        "title": (
            "Folding Camping Kitchen Table - Portable Outdoor Cooking Station "
            "with Heat Resistant Top, Lamp Post for Picnics & Camping Trips"
        ),
        "stored": "112590",
        "kind": "camping_kitchen_table",
        "category_id": "112590",
        "category_name": "Patio & Garden Tables",
        "type_value": "Camping Table",
        "indoor_outdoor": "Outdoor",
        "remove_aspects": CAMPING_REMOVE,
        "room": None,
        "not_kinds": ("side_table",),
        "not_categories": ("38200", "54235"),
        "hint": ("112590", "Patio & Garden Tables"),
        "plausible": {"112590": True, "54235": False, "38200": False},
    },
    {
        "sku": "W465P216801",
        "title": "17 Inch Steel Charcoal Smoker, Heavy Duty Round BBQ Grill for Outdoor Cooking, Black",
        "stored": "151621",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("151621", "Barbecues, Grills & Smokers"),
        "plausible": {"151621": True, "112590": False},
    },
    {
        "sku": "W465P216802",
        "title": (
            "Charcoal Smoker Grill Wood Smoker Grills Outdoor Cooking 16 Inch "
            "Vertical Smoker Portable BBQ Grill for Camping, Black"
        ),
        "stored": "151621",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("151621", "Barbecues, Grills & Smokers"),
        "plausible": {"151621": True, "112590": False},
    },
    {
        # Pre-existing side_table/38200 is out of scope. Do not pin that kind.
        "sku": "W1820P341739",
        "title": (
            "2 Burner BBQ Propane Gas Grill with Side Burner and Side Tables "
            "for Outdoor Cooking Kitchen and Patio Backyard"
        ),
        "stored": "151621",
        "not_kinds": ("camping_kitchen_table",),
        "hint": ("151621", "Barbecues, Grills & Smokers"),
        "plausible": {"151621": True, "112590": False},
    },
    {
        "sku": "W1820P361752",
        "title": (
            "All-in-One Propane Gas Grill with Grilling & Griddling Functions "
            "for Backyard Barbecue & Outdoor Cooking"
        ),
        "stored": "151621",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("151621", "Barbecues, Grills & Smokers"),
        "plausible": {"151621": True, "112590": False},
    },
    {
        "sku": "N707P392140G",
        "title": (
            '80.5" Outdoor Kitchen Island for Big Green Egg & Blackstone Griddle, '
            "Solid Wood Grill Cart Table"
        ),
        "stored": "20725",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("260157", "Kitchen Islands"),
        "plausible": {"20725": True, "112590": True},
    },
    {
        "sku": "N707P235043B",
        "title": (
            "Outdoor Kitchen Island, Rolling Bar Cart & Storage Cabinet, "
            "Farmhouse Solid Wood Outdoor Grill"
        ),
        "stored": "20487",
        "kind": "pantry_cabinet",
        "category_id": "20487",
        "type_value": "Cabinet",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("20487", "Cabinets & Cupboards"),
        "plausible": {"20487": True},
    },
    {
        "sku": "N719P407403B",
        "title": (
            "Outdoor Kitchen Cart Foldable Outdoor Prep Station Portable BBQ Cart "
            "Grill Cart with Foldable Table"
        ),
        "stored": "20487",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("260157", "Kitchen Islands"),
    },
    {
        "sku": "W3190P305785",
        "title": "Outdoor Grill Table for Big Green Egg Style BBQ, Metal Grill Cart with 2 Folding Panels",
        "stored": "177000",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("38204", "Tables"),
    },
    {
        "sku": "W5290P514791",
        "title": "Adjustable Folding Table, Multi-Functional Sturdy Durable Personal Table, No-Assembly Portable",
        "stored": "",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("98044", "Folding Tables"),
    },
    {
        "sku": "N770P200252C",
        "title": "Outdoor Side Table, Patio End Table Cube, MGO Fluted Coffee Table for Outside",
        "stored": "38204",
        "kind": "outdoor_table",
        "category_id": "112590",
        "type_value": "Outdoor Table",
        "indoor_outdoor": "Outdoor",
        "not_kinds": ("camping_kitchen_table",),
        "not_type": "Camping Table",
        "hint": ("112590", "Patio & Garden Tables"),
    },
    {
        "sku": "N773P462387W",
        "title": "Round Outdoor Side Table, 15'' in. W 15'' in. D, End Table",
        "stored": "54235",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "hint": ("112590", "Patio & Garden Tables"),
    },
    {
        "sku": "W311P504501",
        "title": "2PCS Outdoor Camping Chairs Folding/Portable/Adjustable reclining chair",
        "stored": "79684",
        "kind": "outdoor_chair",
        "category_id": "79682",
        "type_value": "Outdoor Chair",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("79682", "Patio Chairs"),
    },
    {
        "sku": "W2753P489186",
        "title": (
            "Portable Double Folding Beach Chair Set with Umbrella, "
            "2-Person Outdoor Camping Chair with Cup Holder"
        ),
        "stored": "79684",
        "kind": "outdoor_chair",
        "category_id": "79682",
        "type_value": "Outdoor Chair",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("79682", "Patio Chairs"),
    },
    {
        # Pre-existing side_table/38200 is out of scope. Do not pin that kind.
        # Hint stays the cooler leaf (Cooler Bag); that is upstream, not camping.
        "sku": "W2932P486017",
        "title": "500lb Capacity Folding Wagon with Push Handle, Side Table, Cooler Bag, and Canopy",
        "stored": "",
        "not_kinds": ("camping_kitchen_table",),
        "hint": ("79691", "Ice Chests & Coolers"),
        "plausible": {"112590": False},
    },
    {
        "sku": "SYNTHETIC-1",
        "title": "Portable Camp Kitchen with Windscreen and Storage Shelf",
        "stored": "",
        "kind": "camping_kitchen_table",
        "category_id": "112590",
        "category_name": "Patio & Garden Tables",
        "type_value": "Camping Table",
        "indoor_outdoor": "Outdoor",
        "remove_aspects": CAMPING_REMOVE,
        "not_kinds": ("side_table", "unknown"),
        "hint": ("112590", "Patio & Garden Tables"),
        "plausible": {"112590": True, "38200": False},
    },
    {
        "sku": "SYNTHETIC-2",
        "title": "Aluminum Camping Table with Mesh Shelf, Folding Picnic Table",
        "stored": "",
        "kind": "camping_kitchen_table",
        "category_id": "112590",
        "category_name": "Patio & Garden Tables",
        "type_value": "Camping Table",
        "indoor_outdoor": "Outdoor",
        "remove_aspects": CAMPING_REMOVE,
        "not_kinds": ("unknown",),
        "hint": ("112590", "Patio & Garden Tables"),
        "not_hints": (("98044", "Folding Tables"),),
        "plausible": {"112590": True, "98044": False},
    },
    {
        "sku": "SYNTHETIC-3",
        "title": "Camping Cook Station with Griddle Plate",
        "stored": "",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("38208", "Furniture"),
        "predicate": False,
    },
    {
        # Bare "cooking station" is not a keyword. Profile still matches because
        # the station phrase sits next to the word "camping".
        "sku": "SYNTHETIC-4",
        "title": "Folding Cooking Station for Camping, Portable Camp Table with Side Shelves",
        "stored": "",
        "kind": "camping_kitchen_table",
        "category_id": "112590",
        "category_name": "Patio & Garden Tables",
        "type_value": "Camping Table",
        "indoor_outdoor": "Outdoor",
        "remove_aspects": CAMPING_REMOVE,
        "not_kinds": ("side_table",),
        "not_categories": ("38200",),
        "hint": ("38204", "Tables"),
        "plausible": {"112590": True, "38200": False},
    },
    {
        "sku": "SYNTHETIC-5",
        "title": "Outdoor Cooking Prep Table with Stainless Top",
        "stored": "",
        "kind": "unknown",
        "not_kinds": ("camping_kitchen_table",),
        "not_categories": ("112590",),
        "hint": ("38204", "Tables"),
        "predicate": False,
    },
]


@pytest.fixture(scope="module")
def matcher():
    return _matcher()


@pytest.mark.parametrize("case", CASES, ids=[case["sku"] for case in CASES])
def test_camping_kitchen_table_row(matcher, case):
    title = case["title"]
    profile = classify_listing_profile(title, case.get("description", ""), case.get("stored", ""))
    hint = matcher.get_keyword_category_hint(title)

    if "predicate" in case:
        assert is_camping_kitchen_title(title) is case["predicate"]
    if "kind" in case:
        assert profile.kind == case["kind"]
    for forbidden in case.get("not_kinds", ()):
        assert profile.kind != forbidden
    if "category_id" in case:
        assert profile.category_id == case["category_id"]
    for forbidden in case.get("not_categories", ()):
        assert profile.category_id != forbidden
    if "category_name" in case:
        assert profile.category_name == case["category_name"]
    if "type_value" in case:
        assert profile.type_value == case["type_value"]
    if "not_type" in case:
        assert profile.type_value != case["not_type"]
    if "indoor_outdoor" in case:
        assert profile.indoor_outdoor == case["indoor_outdoor"]
    if "remove_aspects" in case:
        assert profile.remove_aspects == case["remove_aspects"]
    if "room" in case:
        assert profile.room == case["room"]
    if "hint" in case:
        assert hint == case["hint"]
    for forbidden in case.get("not_hints", ()):
        assert hint != forbidden
    for cid, expected in case.get("plausible", {}).items():
        assert matcher.is_category_plausible_for_text(title, cid) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Folding Camping Tables with Mesh Shelf", True),
        ("Two Camp Kitchens for a Weekend Trip", True),
        ("Heavy Duty Decamping Kitchen Organizer", False),
        ("Cartoon Camping Table Play Set", True),
        # "grille" is not a grill word. The camping phrase still matches.
        ("Folding Camping Kitchen Table with Steel Grille", True),
        ("Steel Grille for Outdoor Cooking", False),
        ("Outdoor Cooking Station with Stainless Top", False),
        ("Portable Cook Station with Side Shelves", False),
        ("Camping Cook Station with Wind Guard", True),
        ("BBQ Camping Kitchen Table", False),
        ("", False),
        (None, False),
    ],
)
def test_is_camping_kitchen_title_word_boundaries(title, expected):
    assert is_camping_kitchen_title(title) is expected


@pytest.mark.parametrize(
    ("sku", "title", "description", "stored", "expect_kind", "expect_hint"),
    [
        (
            "SYNTHETIC-MIXED-CASE",
            "FOLDING cAmPiNg KiTcHeN TaBlE with Heat Resistant Top",
            "",
            "",
            "camping_kitchen_table",
            ("112590", "Patio & Garden Tables"),
        ),
        (
            # Hyphen is not the spaced phrase "camp kitchen".
            "SYNTHETIC-HYPHEN-CAMP-KITCHEN",
            "Portable Camp-Kitchen with Storage Shelf",
            "",
            "",
            "unknown",
            ("38208", "Furniture"),
        ),
        (
            "SYNTHETIC-CHINESE",
            "折叠野营厨房桌 便携户外烹饪台",
            "",
            "",
            "unknown",
            ("38208", "Furniture"),
        ),
        (
            # Exclude words in the description must not win. Title only.
            "SYNTHETIC-DESCRIPTION-GRILL",
            "Folding Camping Kitchen Table with 4 Side Tables",
            "Keep the station away from the grill, smoker, and barbecue cart.",
            "112590",
            "camping_kitchen_table",
            ("112590", "Patio & Garden Tables"),
        ),
    ],
)
def test_camping_kitchen_extra_edge_cases(matcher, sku, title, description, stored, expect_kind, expect_hint):
    assert sku.startswith("SYNTHETIC-")
    profile = classify_listing_profile(title, description, stored)
    assert profile.kind == expect_kind
    assert matcher.get_keyword_category_hint(title) == expect_hint
    if expect_kind == "camping_kitchen_table":
        assert profile.category_id == "112590"
        assert profile.type_value == "Camping Table"
        assert profile.remove_aspects == CAMPING_REMOVE
        assert profile.kind != "side_table"
        assert matcher.is_category_plausible_for_text(title, "38200") is False
        assert matcher.is_category_plausible_for_text(title, "112590") is True
    else:
        assert profile.kind != "camping_kitchen_table"
        assert is_camping_kitchen_title(title) is False
