from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.ebay_category_matcher import EbayCategoryMatcher


class _DummyOauth:
    def get_application_token(self):
        raise AssertionError("network call should not be needed for fallback tests")


def test_ping_pong_table_fallback_category_avoids_dining_set_match():
    matcher = EbayCategoryMatcher(_DummyOauth())

    category_id, category_name = matcher._fallback_category(
        "4.5ft Foldable Table Tennis Table Set with Net and 2 Paddles"
    )

    assert category_id == "97075"
    assert category_name == "Tables"


def test_ping_pong_table_category_plausibility_accepts_table_tennis_category():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "4.5ft Foldable Table Tennis Table Set with Net and 2 Paddles"

    assert matcher.is_category_plausible_for_text(title, "97075", "Tables") is True
    assert matcher.is_category_plausible_for_text(title, "107578", "Dining Sets") is False


def test_coffee_table_set_category_plausibility_rejects_dining_set_match():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "2-Piece Round Nesting Coffee Table Set Black Metal Modern Indoor Outdoor"

    assert matcher.is_category_plausible_for_text(title, "38204", "Coffee Tables") is True
    assert matcher.is_category_plausible_for_text(title, "107578", "Dining Sets") is False


def test_coffee_table_with_patio_keywords_is_not_canonicalized_to_patio_table():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = (
        "2-Piece Round Nesting Coffee Table Set, Modern Black Metal Side & End Tables, "
        "Space-Saving Accent Table Set for Living Room, Bedroom, Balcony, Patio, Indoor & Outdoor Use"
    )

    assert matcher.canonicalize_category(title, "38204", "Coffee Tables") == ("38204", "Coffee Tables")


def test_sofa_side_table_nightstand_not_remapped_to_sofas_or_beds():
    """2026-07-13 live incident: 'Storage Bedside Table and Sofa Side Table'
    was remapped 38199 -> 175758 (Bed Frames) by a substring 'storage bed'
    match, and console tables were remapped to 38208 by the bare sofa word."""
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "Solid Wood Nightstand with Two Drawers and Pull-out Panel Storage Bedside Table and Sofa Side Table"

    category_id, _ = matcher.canonicalize_category(title, "38199", "Nightstands")

    assert category_id in (None, "38199")


def test_console_sofa_table_behind_couch_not_remapped_to_sofas():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "60 Inch Narrow Console Table with Built-in Power Outlet, Farmhouse Sofa Table Behind Couch, Entryway"

    category_id, _ = matcher.canonicalize_category(title, "38204", "Tables")

    assert category_id != "38208"


def test_real_sofa_still_remapped_to_sofas():
    matcher = EbayCategoryMatcher(_DummyOauth())
    title = "71 Inch 3 Seater Sofa Corduroy Fabric Deep Seat Couch Comfy Loveseat"

    category_id, category_name = matcher.canonicalize_category(title, "38204", "Tables")

    assert category_id == "38208"
    assert category_name == "Sofas, Armchairs & Couches"
