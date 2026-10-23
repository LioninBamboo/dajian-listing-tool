"""T3 BOM / package-line parsing for auto gold-template helpers."""

from __future__ import annotations

import pytest

from src.plugins.terapeak_research.intelligence_service import landed_cost_from_breakdown
from src.services.auto_technical_prompt import (
    _title_piece_count,
    build_auto_gold_description,
    build_package_lines_without_bom,
    extract_source_bom_lines,
    extract_source_mpn,
)
from src.services.pricing_engine import PricingEngine
from src.utils.store_profile import StoreProfile


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
    assert landed_cost_from_breakdown({"product_price": 0, "base_cost": 54.7}) == 0.0
    assert landed_cost_from_breakdown({}) == 0.0


def test_s3_giga_title_line_is_not_treated_as_bom():
    src = "4pcs Running Boards For 2024 2025 Toyota Tacoma Double Cab/Crew Cab Side Steps"
    assert extract_source_bom_lines(src) == []


def _aqua_profile():
    return StoreProfile(
        store_kind="auto",
        brand_name="AquaRides",
        listing_channel="trading",
        ebay_site_id="100",
        qc_profile="motors",
        scheduler_profile="ops",
        brand_tagline="PERFORMANCE AUTO PARTS",
        description_footer_line1="✦ Ships from US Warehouse ✦",
        quality_footer_marker="ships from",
        template_style="auto_technical",
        force_house_brand=True,
        category_tree_id="100",
    )


def test_socket_set_without_count_raises_at_gold_description():
    with pytest.raises(ValueError, match="package includes"):
        build_auto_gold_description(
            title="Socket Set",
            source_description="",
            aspects={"Type": ["Socket Set"]},
            profile=_aqua_profile(),
        )


def test_gold_copy_contains_only_source_grounded_aspect_values():
    html = build_auto_gold_description(
        title="Roof Rack Cargo Basket 50 x 36 Universal Rooftop Luggage",
        source_description="Carbon steel cargo basket.",
        aspects={
            "Type": ["Roof Rack Cargo Basket"],
            "Material": ["Carbon Steel"],
            "Finish": ["Matte"],
        },
        profile=_aqua_profile(),
    )
    low = html.lower()
    assert "factory-style" not in low
    assert "purpose-built" not in low
    assert "installation on the vehicle" not in low
    assert "matches common" not in low
    assert "Carbon Steel" in html
    assert "Matte" in html


def test_m7_w3921s00002_oversize_freight_rate_never_drops_cost():
    product = 220.0
    shipping = 16.0
    oversize = PricingEngine.calculate_dajian_cost(product, shipping, True)
    express = PricingEngine.calculate_dajian_cost(product, shipping, False)
    stored = 254.59
    breakdown = {
        "product_price": product,
        "shipping_cost": shipping,
        "base_cost": product + shipping,
        "logistics_insurance": oversize["logistics_insurance"],
        "logistics_insurance_rate": 0.05,
        "total_dajian_cost": stored,
    }
    got = landed_cost_from_breakdown(breakdown, specs={}, attributes={})
    assert got >= stored
    assert got == max(oversize["total_dajian_cost"], stored)
    assert got > express["total_dajian_cost"]

    no_ratio = {
        "product_price": product,
        "shipping_cost": shipping,
        "total_dajian_cost": stored,
    }
    got_floor = landed_cost_from_breakdown(no_ratio, specs={}, attributes={})
    assert got_floor >= stored
