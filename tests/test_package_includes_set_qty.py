"""PACKAGE INCLUDES main qty must follow set-of-N for singular Type."""

from scripts.audit_fix_active_listings import (
    _build_package_includes_copy,
    _resolve_package_main_qty,
)


def test_build_package_includes_uses_set_of_2_for_dining_chair():
    title = "Dining Chairs Set of 2 Solid Ash Wood Mid-Century Modern Walnut"
    aspects = {
        "Type": ["Dining Chair"],
        "Number of Items in Set": ["2"],
        "Set Includes": ["Chairs"],
    }
    copy = _build_package_includes_copy(title, "Comes as a set of 2 matching chairs", aspects, "Yes")
    assert copy.startswith("2 x Dining Chair")
    assert "1 x Hardware Kit" in copy
    assert "1 x Assembly Instructions" in copy


def test_resolve_package_main_qty_keeps_one_for_named_set_type():
    assert _resolve_package_main_qty(
        "10Pcs Premium Wood Chisel Set",
        {"Type": ["Wood Chisel Set"], "Number of Items in Set": ["10"]},
    ) == 1


def test_resolve_package_main_qty_from_title_when_aspect_missing():
    assert _resolve_package_main_qty(
        "Outdoor Adirondack Chair Set of 2",
        {"Type": ["Adirondack Chair"]},
    ) == 2


def test_resolve_package_main_qty_ignores_n_piece_mixed_set():
    assert _resolve_package_main_qty(
        "3 Piece Sectional Sofa",
        {"Type": ["Sectional"], "Number of Items in Set": ["3"]},
    ) == 1
    assert _resolve_package_main_qty(
        "3-Piece Coffee Table with End Tables",
        {"Type": ["Coffee Table"], "Number of Items in Set": ["3"]},
    ) == 1
    assert _resolve_package_main_qty(
        "3 Piece Sectional Sofa",
        {"Type": ["Sectional"]},
    ) == 1


def test_build_package_includes_uses_one_sectional_for_n_piece():
    copy = _build_package_includes_copy(
        "3 Piece Sectional Sofa",
        "",
        {"Type": ["Sectional"], "Number of Items in Set": ["3"]},
        None,
    )
    assert copy.startswith("1 x Sectional")
