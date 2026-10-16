"""Tests for the GrovePop garden template — fully deterministic layout from LLM text."""

import dataclasses

from src.services.garden_lifestyle_prompt import (
    build_garden_lifestyle_system_prompt,
    finalize_garden_lifestyle_listing,
)
from src.utils.store_profile import StoreProfile


def _grovepop_profile():
    return dataclasses.replace(
        StoreProfile(),
        brand_name="GrovePop",
        brand_tagline="OUTDOOR LIVING · GARDEN · PET",
        description_footer_line1="✦ Ships from US Warehouse ✦",
        description_footer_line2="Quality Guaranteed • Fast US Shipping",
        quality_footer_marker="ships from",
        template_style="garden_lifestyle",
        force_house_brand=True,
    )


_DATA = {
    "title": "MGO Garden Planter Box Rust Outdoor",
    "intro": "A square MGO planter box for everyday outdoor planting.",
    "features": [
        "Sturdy MGO Build: durable, natural-textured finish.",
        "Square Design: clean modern silhouette.",
        "Freestanding: ready to use, no assembly.",
    ],
    "perfect_for": "Patios, balconies and garden beds.",
    "aspects": {
        "Type": ["Planter"], "Material": ["Magnesium Oxide (MGO)"], "Color": ["Rust"],
        "Item Length": ["15.7 in"], "Item Width": ["15.7 in"], "Item Height": ["14.0 in"],
        "Item Weight": ["10.78 lbs"], "Brand": ["SomethingElse"],
    },
}


class TestFinalize:
    def _out(self):
        return finalize_garden_lifestyle_listing(dict(_DATA), _grovepop_profile())["description"]

    def test_single_banner_and_footer(self):
        d = self._out()
        assert d.count("GROVEPOP") == 1
        assert d.count("Ships from US Warehouse") == 1
        assert "f8f9fa" not in d.lower()                      # no furniture shell

    def test_deterministic_chrome_and_layout(self):
        d = self._out()
        assert "2e5d43" in d.lower()                          # garden green
        assert "cbab6b" in d.lower()                          # sand accent (banner/footer/hero)
        assert d.count("<li") == 3                            # KEY FEATURES from the features list
        assert "Perfect For" in d and "f3f7f2" in d.lower()   # cream PERFECT FOR card
        assert "<table" not in d
        assert "Dimensions (L × W × H)" in d                  # folded hero dims
        assert "Sturdy MGO Build" in d                        # feature text rendered
        assert len(d) < 4000                                  # Inventory cap

    def test_house_brand_forced(self):
        out = finalize_garden_lifestyle_listing(dict(_DATA), _grovepop_profile())
        assert out["aspects"]["Brand"] == ["GrovePop"]

    def test_dimensions_numbers_present_for_qc(self):
        # The measurement QC requires the Item L/W/H numbers in the description.
        d = self._out()
        assert "15.7" in d and "14.0" in d

    def test_empty_content_still_valid(self):
        out = finalize_garden_lifestyle_listing(
            {"title": "T", "aspects": {"Material": ["MGO"]}}, _grovepop_profile()
        )["description"]
        assert out.count("GROVEPOP") == 1                     # banner + footer still render


class TestPrompt:
    def test_prompt_asks_for_text_fields_not_html(self):
        sp = build_garden_lifestyle_system_prompt(_grovepop_profile()).lower()
        assert "you do not write any html" in sp
        assert '"perfect_for"' in sp and '"features"' in sp and '"intro"' in sp
        assert "weatherproof" in sp                           # named as a forbidden invented claim
