"""T3 BOM / package-line parsing for auto gold-template helpers."""

from __future__ import annotations

from src.plugins.terapeak_research.intelligence_service import landed_cost_from_breakdown
from src.services.auto_technical_prompt import (
    _title_piece_count,
    build_package_lines_without_bom,
    extract_source_bom_lines,
    extract_source_mpn,
)


def test_t3_b1_real_sku_w465p451141_seven_piece_wrench_set():
    title = "7-Piece Wrench Set"
    assert _title_piece_count(title, {}) == 7
    lines = build_package_lines_without_bom(
        title=title,
        item_type="Wrench Set",
        aspects={},
        source_description="",
    )
    assert lines is not None
    assert lines[0] == "7 x Wrench"
    assert not any("Table Set" in line for line in lines)


def test_t3_b2_source_html_bom_lines_not_merged_synthetic():
    assert extract_source_bom_lines("1pc utility knife<br>2pcs blades") == [
        "1pc utility knife",
        "2pcs blades",
    ]


def test_t3_b3_inch_and_drawer_counts_are_not_piece_counts_synthetic():
    title = "Tool Chest 26 Inch, 5 Drawers"
    assert _title_piece_count(title, {}) is None
    lines = build_package_lines_without_bom(
        title=title,
        item_type="Tool Chest",
        aspects={},
    )
    assert lines == ["1 x Tool Chest"]


def test_t3_b4_set_without_number_blocks_synthetic():
    title = "Socket Set"
    assert _title_piece_count(title, {}) is None
    assert build_package_lines_without_bom(title=title, item_type="Socket Set", aspects={}) is None


def test_t3_b5_set_of_four_wheel_chocks_synthetic():
    title = "Set of 4 Wheel Chocks"
    assert _title_piece_count(title, {}) == 4


def test_t3_b6_extract_source_mpn_from_snapshot_docstring():
    assert extract_source_mpn("MPN: 49901095-23") == "49901095-23"
    assert extract_source_mpn("Steel hitch, no identifier.") == ""


def test_t3_b7_two_pack_does_not_take_voltage_synthetic():
    title = "2 Pack 12V Fan"
    assert _title_piece_count(title, {}) == 2


def test_synthetic_mm_size_is_not_a_piece_count():
    assert _title_piece_count("Grinder Stand for 150mm 180mm 230mm Grinders", {}) is None


def test_synthetic_quoted_inch_is_not_a_piece_count():
    assert _title_piece_count('Hitch Pin 5/8" Heavy Duty', {}) is None


def test_c5_landed_cost_excludes_raw_base_cost_fallback():
    assert landed_cost_from_breakdown({"base_cost": 54.7}) == 0.0
    assert landed_cost_from_breakdown({}) == 0.0
