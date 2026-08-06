"""Tests for the auto-parts / tools listing prompt + finalizer (P0-A2)."""

import dataclasses

import pytest

from src.services.auto_technical_prompt import (
    AUTO_MODE_FITMENT,
    AUTO_MODE_TOOL,
    build_auto_technical_system_prompt,
    build_auto_technical_user_prompt,
    detect_auto_mode,
    finalize_auto_technical_listing,
)
from src.utils.store_profile import StoreProfile


def _auto_profile(force_house_brand=True):
    return dataclasses.replace(
        StoreProfile(),
        brand_name="AquaRides",
        brand_tagline="PERFORMANCE AUTO PARTS & ACCESSORIES",
        description_footer_line1="✦ Ships from US Warehouse ✦",
        description_footer_line2="Quality Guaranteed • Fast US Shipping",
        quality_footer_marker="ships from",
        template_style="auto_technical",
        force_house_brand=force_house_brand,
    )


class _CompatObj:
    mode = "by_year"
    compatible_products = [
        {"compatibilityProperties": [{"name": "Make", "value": "Ford"}]}
    ]


class TestDetectMode:
    def test_part_by_keyword(self):
        assert detect_auto_mode(
            "Class 3 Trailer Hitch 2 Inch Receiver", "rear receiver",
            {"Type": ["Receiver Hitch"]},
        ) == AUTO_MODE_FITMENT

    def test_tool_by_keyword(self):
        assert detect_auto_mode(
            "1/2 in Cordless Impact Wrench Kit", "high torque", {}
        ) == AUTO_MODE_TOOL

    def test_running_board_is_a_part_not_a_tool(self):
        # False-friend guard: "running board" must not trip a tool signal.
        assert detect_auto_mode("Running Board Nerf Bar Side Step", "", {}) == AUTO_MODE_FITMENT

    def test_tool_word_but_part_present_stays_part(self):
        assert detect_auto_mode("Brake Rotor Kit with wrench", "", {}) == AUTO_MODE_FITMENT

    def test_ambiguous_defaults_to_fitment(self):
        assert detect_auto_mode("Universal Rubber Floor Mats", "", {}) == AUTO_MODE_FITMENT

    def test_structured_compatibility_forces_fitment(self):
        assert detect_auto_mode("anything at all", "", {}, _CompatObj()) == AUTO_MODE_FITMENT

    def test_list_compatibility_forces_fitment(self):
        entries = [{"compatibilityProperties": [{"name": "Make", "value": "Ford"}]}]
        assert detect_auto_mode("anything", "", {}, entries) == AUTO_MODE_FITMENT


class TestSystemPrompt:
    def test_tool_prompt_has_no_vehicle_fitment(self):
        sp = build_auto_technical_system_prompt(_auto_profile(), AUTO_MODE_TOOL)
        assert "TOOLS" in sp
        assert "universal" in sp.lower()          # tools are universal, no YMM
        assert "WHAT'S IN THE BOX" in sp

    def test_fitment_prompt_warns_against_inventing_fitment(self):
        sp = build_auto_technical_system_prompt(_auto_profile(), AUTO_MODE_FITMENT)
        assert "PARTS" in sp
        assert "do not invent" in sp.lower() or "not invent" in sp.lower()
        assert "compatibility" in sp.lower()

    def test_user_prompt_carries_source_facts(self):
        up = build_auto_technical_user_prompt(
            title="Hitch", description="steel receiver", mode=AUTO_MODE_FITMENT,
            attributes={"Material": ["Steel"]}, specs={"weight": "30 lb"},
            market_intel={"top_keywords": ["trailer hitch"], "price_stats": {"min": 40, "max": 90, "avg": 60}},
        )
        assert "Hitch" in up and "Steel" in up and "trailer hitch" in up


class TestFinalize:
    def test_forces_house_brand_over_source(self):
        out = finalize_auto_technical_listing(
            {"title": "T", "description": "<div>x</div>",
             "aspects": {"Brand": ["Bosch"], "Type": ["Hitch"]}},
            _auto_profile(force_house_brand=True), AUTO_MODE_FITMENT,
        )
        assert out["aspects"]["Brand"] == ["AquaRides"]
        assert out["aspects"]["Type"] == ["Hitch"]

    def test_keeps_source_brand_when_not_forcing(self):
        out = finalize_auto_technical_listing(
            {"title": "T", "description": "<div>x</div>", "aspects": {"Brand": ["Bosch"]}},
            _auto_profile(force_house_brand=False), AUTO_MODE_TOOL,
        )
        assert out["aspects"]["Brand"] == ["Bosch"]

    def test_title_clamped_to_80(self):
        out = finalize_auto_technical_listing(
            {"title": "X" * 120, "description": "<div>x</div>", "aspects": {}},
            _auto_profile(), AUTO_MODE_FITMENT,
        )
        assert len(out["title"]) == 80

    def test_footer_appended_once(self):
        first = finalize_auto_technical_listing(
            {"title": "T", "description": "<div>x</div>", "aspects": {}},
            _auto_profile(), AUTO_MODE_FITMENT,
        )
        assert "Ships from US Warehouse" in first["description"]
        again = finalize_auto_technical_listing(
            {"title": "T", "description": first["description"], "aspects": {}},
            _auto_profile(), AUTO_MODE_FITMENT,
        )
        assert again["description"].count("Ships from US Warehouse") == 1

    def test_mode_recorded_and_features_defaulted(self):
        out = finalize_auto_technical_listing(
            {"title": "T", "description": "<div>x</div>", "aspects": {}},
            _auto_profile(), AUTO_MODE_TOOL,
        )
        assert out["mode"] == AUTO_MODE_TOOL
        assert out["features"] == []
