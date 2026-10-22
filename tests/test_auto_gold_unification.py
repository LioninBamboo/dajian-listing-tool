"""Gold description is the only AquaRides template — generator, QC, align, quality gate."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from src.services.auto_technical_prompt import (
    build_auto_gold_description,
    looks_like_gold_template,
)
from src.utils.listing_quality_gate import ensure_store_description_template
from src.utils.store_profile import StoreProfile


def _auto_profile():
    return StoreProfile(
        brand_name="AquaRides",
        brand_tagline="PERFORMANCE AUTO PARTS & ACCESSORIES",
        description_footer_line1="✦ Ships from US Warehouse ✦",
        description_footer_line2="Quality Guaranteed • Fast US Shipping • Trusted Seller",
        quality_banner_marker="aquarides",
        quality_footer_marker="ships from",
        template_style="auto_technical",
        store_kind="auto",
        force_house_brand=True,
    )


def _source():
    return {
        "title": "Class 3 Trailer Hitch 2 Inch Receiver for Toyota Tacoma",
        "source_description": "Carbon steel Class 3 hitch with 2 inch receiver. Rear mount. Powder coated black.",
        "attributes": {"Material": "Carbon Steel", "Color": "Black"},
        "specs": {
            "Assembled Length (in.)": "46.5",
            "Assembled Width (in.)": "12.0",
            "Assembled Height (in.)": "6.0",
        },
        "aspects": {
            "Type": ["Trailer Hitch"],
            "Material": ["Carbon Steel"],
            "Finish": ["Black"],
            "Fitment Type": ["Vehicle Specific Fit"],
            "Placement on Vehicle": ["Rear"],
            "Item Length": ["46.5 in"],
            "Item Width": ["12.0 in"],
            "Item Height": ["6.0 in"],
            "Assembly Required": ["Yes"],
        },
    }


def test_build_auto_gold_description_is_w3611_chrome_not_furniture_shell():
    src = _source()
    desc = build_auto_gold_description(profile=_auto_profile(), **src)
    assert looks_like_gold_template(desc)
    assert "#1f2329" in desc
    assert "Attribute" in desc
    assert "AQUARIDES" in desc
    assert "KEY FEATURES" in desc
    assert "overwhelming the room" not in desc.lower()
    assert "styling the space" not in desc.lower()
    assert "Assembly Required" not in desc
    assert "border-left:4px solid" not in desc


def test_looks_like_gold_template_rejects_furniture_decor():
    furniture = (
        "<div>AQUARIDES KEY FEATURES SPECIFICATIONS PACKAGE INCLUDES"
        " without overwhelming the room. Ships from US Warehouse</div>"
        '<table><tr style="background:#1f2329;"><th>Attribute</th></tr></table>'
    )
    assert looks_like_gold_template(furniture) is False


def test_qwen_auto_compose_uses_gold_builder():
    from qwen_optimizer import QwenOptimizer

    src = _source()
    result = QwenOptimizer.compose_auto_technical_result(
        data={
            "title": src["title"],
            "aspects": src["aspects"],
            "features": ["Steel hitch"],
            "description": "",
        },
        original_title=src["title"],
        original_description=src["source_description"],
        attributes=src["attributes"],
        specs=src["specs"],
        profile=_auto_profile(),
        mode="fitment",
    )
    assert looks_like_gold_template(result["description"])
    assert "overwhelming the room" not in result["description"].lower()


def test_quality_gate_rebuilds_auto_furniture_shell_to_gold():
    src = _source()
    furniture = (
        '<div style="max-width:900px">AQUARIDES KEY FEATURES '
        "without overwhelming the room SPECIFICATIONS PACKAGE INCLUDES "
        "Ships from US Warehouse</div>"
    )
    rebuilt = ensure_store_description_template(
        furniture,
        title=src["title"],
        source_description=src["source_description"],
        attributes=src["attributes"],
        specs=src["specs"],
        aspects=src["aspects"],
        profile=_auto_profile(),
    )
    assert looks_like_gold_template(rebuilt)
    assert "overwhelming the room" not in rebuilt.lower()
