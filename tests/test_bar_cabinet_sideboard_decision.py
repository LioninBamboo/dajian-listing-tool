"""Bar / wine / liquor cabinets remap to Sideboards, not Wine Racks."""

from src.utils.category_decision import SIDEBOARD, resolve_category_decision


def test_wine_cabinet_on_wine_rack_leaf_targets_sideboard():
    decision = resolve_category_decision(
        "Wine Cabinet with Glass Doors 18-Bottle Storage",
        "",
        "",
        "20689",
        "Wine Racks & Bottle Holders",
    )
    assert decision.kind == "sideboard"
    assert decision.category_id == SIDEBOARD
    assert decision.reason == "bar_cabinet_sideboard"


def test_bar_cabinet_already_on_sideboard_stays():
    decision = resolve_category_decision(
        "Black LED Bar Cabinet with Glass Doors",
        "",
        "",
        SIDEBOARD,
        "Sideboards & Buffets",
    )
    assert decision.kind == "keep"
    assert decision.reason == "bar_cabinet_sideboard_lock"
