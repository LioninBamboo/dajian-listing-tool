"""Tests for the GrovePop garden/outdoor lifestyle template (deterministic chrome)."""

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


_ASPECTS = {
    "Type": ["Planter"], "Material": ["Magnesium Oxide (MGO)"], "Color": ["Rust"],
    "Item Length": ["15.7 in"], "Item Width": ["15.7 in"], "Item Height": ["14 in"],
    "Item Weight": ["10.78 lbs"], "Brand": ["SomethingElse"],
}
_BODY = "<div><p>intro</p><ul><li>feature</li></ul></div>"


class TestFinalize:
    def _out(self):
        return finalize_garden_lifestyle_listing(
            {"title": "MGO Garden Planter", "description": _BODY, "aspects": dict(_ASPECTS)},
            _grovepop_profile(),
        )["description"]

    def test_single_banner_and_footer_no_double_wrap(self):
        d = self._out()
        assert d.count("GROVEPOP") == 1                      # the bug we're fixing: one banner
        assert d.count("Ships from US Warehouse") == 1
        assert "f8f9fa" not in d.lower()                     # not the furniture navy/gold shell

    def test_deterministic_chrome_present(self):
        d = self._out()
        assert "<table" in d and d.count("<table") == 1      # legible spec table
        assert "2e5d43" in d.lower()                         # garden green palette
        assert "Dimensions (L × W × H)" in d                 # dimensions folded to one row
        assert ">Item Length<" not in d

    def test_house_brand_forced(self):
        out = finalize_garden_lifestyle_listing(
            {"title": "T", "description": _BODY, "aspects": dict(_ASPECTS)},
            _grovepop_profile(),
        )
        assert out["aspects"]["Brand"] == ["GrovePop"]

    def test_banner_idempotent(self):
        first = self._out()
        again = finalize_garden_lifestyle_listing(
            {"title": "T", "description": first, "aspects": dict(_ASPECTS)},
            _grovepop_profile(),
        )["description"]
        assert again.count("GROVEPOP") == 1                  # re-finalize does not double


class TestPrompt:
    def test_prompt_forbids_chrome_and_fabrication(self):
        sp = build_garden_lifestyle_system_prompt(_grovepop_profile()).lower()
        assert "do not render a banner" in sp
        assert "perfect for" in sp
        assert "weatherproof" in sp                          # named as a forbidden invented claim
