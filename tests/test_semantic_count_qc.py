"""Bug 3: semantic_count false positives from word numerals and unnumbered plurals."""

from __future__ import annotations

import pytest

from src.utils.listing_fact_sheet import (
    _count_claim_in_text,
    compare_fact_sheets,
    parse_noun_count,
)


PARSE_ROWS = (
    ("three smooth-glide drawers", "drawer", 3, 1),
    ("3 Deep Drawers", "drawer", 3, None),
    ("Kids 3 Drawer Dresser", "drawer", 3, None),
    ("3-Drawer Dresser", "drawer", 3, None),
    ("Couch with Cup Holders & Pillows", "pillow", None, 1),
    ("two matching throw pillows", "pillow", 2, None),
    ("Glass Doors and Drawer", "drawer", 1, 2),
    ("Double Glass Doors", "door", 2, 1),
    ("47 Inch Chairs", "chair", None, 47),
    ("8-inch wheels", "wheel", None, 8),
    ("8-in wheels", "wheel", None, 8),
    ("3-in-1 drawers", "drawer", None, 1),
    ("3-in-1 drawer", "drawer", None, 1),
    ("3–in–1 drawers", "drawer", None, 1),
    ("4 drawers, 3-in-1 design", "drawer", 4, 1),
    ("two glass-door compartments", "door", None, 3),
    ("stable base", "table", None, None),
    ("eleven shelves", "shelf", 11, 1),
    ("pair of pillows", "pillow", 2, 1),
    ("single drawer", "drawer", 1, 2),
    ("one of the shelves", "shelf", None, 1),
    ("2-sided panel", "panel", None, 2),
    ("Double Bed with Storage", "bed", None, 2),
)


@pytest.mark.parametrize("text,noun,expected,forbidden", PARSE_ROWS)
def test_parse_noun_count_table(text, noun, expected, forbidden):
    parsed = parse_noun_count(text, noun)
    assert parsed == expected
    if forbidden is not None:
        assert parsed != forbidden


def test_set_of_chairs_mismatch_still_flags():
    source = {"materials": [], "features": [], "counts": {"chairs": 2}, "capacity": None, "certifications": [], "dimensions": {}}
    live = dict(source, counts={"chairs": 4})
    violations = compare_fact_sheets(
        source,
        live,
        source_text="Set of 2 Chairs",
        live_text="Set of 4 Chairs",
    )
    assert any(v["claim_type"] == "semantic_count" for v in violations)


def test_shelf_digit_mismatch_still_flags():
    source = {"materials": [], "features": [], "counts": {"shelves": 3}, "capacity": None, "certifications": [], "dimensions": {}}
    live = dict(source, counts={"shelves": 5})
    violations = compare_fact_sheets(
        source,
        live,
        source_text="3 shelves",
        live_text="5 shelves",
    )
    assert any(v["claim_type"] == "semantic_count" for v in violations)


def test_n707_word_numerals_are_not_one():
    source = {"materials": [], "features": [], "counts": {"drawers": 3, "shelves": 3}, "capacity": None, "certifications": [], "dimensions": {}}
    live = dict(source, counts={"drawers": 1, "shelves": 2})
    violations = compare_fact_sheets(
        source,
        live,
        source_text="[2Slide Barn Doors + 3 Deep Drawers] It includes three drawers, two cabinets with three interior shelves",
        live_text="Plus, three smooth-glide drawers for organizing tableware. Inside, there are three adjustable shelves",
    )
    assert not any(v["claim_type"] == "semantic_count" for v in violations)


def test_w808_unnumbered_or_hyphenated_drawers_not_one():
    source = {"materials": [], "features": [], "counts": {"drawers": 3}, "capacity": None, "certifications": [], "dimensions": {}}
    live = dict(source, counts={"drawers": 1})
    title_only = compare_fact_sheets(
        source,
        live,
        source_text="Kids 3 Drawer Dresser 3-Drawer Dresser",
        live_text="Sage Wood Nightstand with Anti-Tip Kit",
    )
    assert not any(v["claim_type"] == "semantic_count" for v in title_only)
    with_desc = compare_fact_sheets(
        source,
        live,
        source_text="Kids 3 Drawer Dresser 3-Drawer Dresser",
        live_text="Sage Wood Nightstand with Anti-Tip Kit 3-Drawer dresser and drawers",
    )
    assert not any(v["claim_type"] == "semantic_count" for v in with_desc)


def test_w5156_plural_pillows_are_unknown():
    source = {"materials": [], "features": [], "counts": {"pillows": 2}, "capacity": None, "certifications": [], "dimensions": {}}
    live = dict(source, counts={"pillows": 1})
    violations = compare_fact_sheets(
        source,
        live,
        source_text="two matching throw pillows",
        live_text="Couch with Cup Holders & Pillows",
    )
    assert not any(v["claim_type"] == "semantic_count" for v in violations)


def test_w2557_source_sheet_two_not_trusted_without_text():
    source = {"materials": [], "features": [], "counts": {"drawers": 2}, "capacity": None, "certifications": [], "dimensions": {}}
    live = dict(source, counts={"drawers": 1})
    violations = compare_fact_sheets(
        source,
        live,
        source_text="Double Glass Doors and Drawer. Spacious bottom drawer",
        live_text="Glass Doors and Drawer",
    )
    assert not any(v["claim_type"] == "semantic_count" for v in violations)


def test_n710_ambiguous_doors_not_flagged():
    source = {"materials": [], "features": [], "counts": {"doors": 3}, "capacity": None, "certifications": [], "dimensions": {}}
    live = dict(source, counts={"doors": 2})
    violations = compare_fact_sheets(
        source,
        live,
        source_text="two glass-door compartments, woven rattan doors",
        live_text="2 Drawers & Glass Door Storage",
    )
    assert not any(v["claim_type"] == "semantic_count" for v in violations)


def test_none_inputs_do_not_crash():
    assert parse_noun_count(None, "drawer") is None  # type: ignore[arg-type]
    assert parse_noun_count("", None) is None  # type: ignore[arg-type]
    source = {"materials": [], "features": [], "counts": {}, "capacity": None, "certifications": [], "dimensions": {}}
    assert compare_fact_sheets(source, source, live_text=None, source_text=None) == []


def test_dimension_units_are_not_count_claims():
    assert _count_claim_in_text(8, "wheel", "8-inch wheels") is False
    assert _count_claim_in_text(8, "wheel", "8-in wheels") is False
    assert _count_claim_in_text(47, "chair", "47 Inch Chairs") is False
    assert _count_claim_in_text(1, "shelf", "one of the shelves") is False
    assert _count_claim_in_text(2, "panel", "2-sided panel") is False
    assert _count_claim_in_text(2, "bed", "Double Bed with Storage") is False
    assert _count_claim_in_text(1, "drawer", "3-in-1 drawers") is False
    assert _count_claim_in_text(3, "drawer", "3-in-1 drawers") is False
    assert _count_claim_in_text(1, "drawer", "3–in–1 drawers") is False
    assert _count_claim_in_text(4, "drawer", "4 drawers, 3-in-1 design") is True


def test_n_in_1_does_not_raise_semantic_count():
    source = {"materials": [], "features": [], "counts": {"drawers": 4}, "capacity": None, "certifications": [], "dimensions": {}}
    live_sheet_one = dict(source, counts={"drawers": 1})
    live_sheet_real = dict(source, counts={"drawers": 4})
    for live_text, live in (
        ("3-in-1 drawers", live_sheet_one),
        ("3-in-1 drawer", live_sheet_real),
        ("3–in–1 drawers", live_sheet_real),
        ("Convertible 3-in-1 drawer chest", live_sheet_one),
    ):
        violations = compare_fact_sheets(
            source,
            live,
            source_text="Dresser with 4 drawers",
            live_text=live_text,
        )
        assert not any(v["claim_type"] == "semantic_count" for v in violations)


def test_cached_sheet_one_without_text_corroboration_is_skipped():
    source = {"materials": [], "features": [], "counts": {"drawers": 3}, "capacity": None, "certifications": [], "dimensions": {}}
    live = dict(source, counts={"drawers": 1})
    violations = compare_fact_sheets(
        source,
        live,
        source_text="Kids 3 Drawer Dresser",
        live_text="Nightstand with drawers",
    )
    assert not any(v["claim_type"] == "semantic_count" for v in violations)
