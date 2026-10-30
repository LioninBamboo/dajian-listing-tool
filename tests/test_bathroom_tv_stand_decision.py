"""Bathroom vanity → Bathroom Cabinets; TV stand → TV Stands."""

from src.utils.category_decision import resolve_category_decision


def test_bathroom_vanity_on_display_cabinet_targets_bathroom_cabinets():
    decision = resolve_category_decision(
        "24 Inch Bathroom Vanity with Ceramic Sink Modern White",
        "",
        "",
        "20487",
        "Cabinets & Cupboards",
    )
    assert decision.kind == "vanity"
    assert decision.category_id == "176991"
    assert decision.reason == "bathroom_vanity_head"


def test_bathroom_vanity_already_on_bathroom_cabinets_stays():
    decision = resolve_category_decision(
        "30 Inch Bathroom Vanity without Top Sink Storage Cabinet",
        "",
        "",
        "176991",
        "Bathroom Cabinets",
    )
    assert decision.kind == "keep"
    assert decision.reason in {"vanity_lock", "stored_leaf_lock", "stored_head_fit"}


def test_tv_stand_on_display_cabinet_targets_tv_stands():
    decision = resolve_category_decision(
        "78 Inch Mid Century Modern TV Stand Fluted Walnut Media Console",
        "",
        "",
        "20487",
        "Cabinets & Cupboards",
    )
    assert decision.kind == "tv_stand"
    assert decision.category_id == "20488"
    assert decision.reason == "tv_stand_head"


def test_tv_stand_already_on_tv_stands_stays():
    decision = resolve_category_decision(
        "NATURAL WOOD TV-STAND 59 INCH SIDEBOARD CABINET",
        "",
        "",
        "20488",
        "TV Stands & Entertainment Units",
    )
    assert decision.kind == "keep"
    assert decision.reason in {"tv_stand_lock", "stored_leaf_lock", "stored_head_fit"}
