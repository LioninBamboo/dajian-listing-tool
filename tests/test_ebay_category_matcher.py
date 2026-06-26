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
