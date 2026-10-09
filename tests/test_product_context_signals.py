"""Tests for the shared indoor/outdoor context signals.

These signals were duplicated across ebay_category_matcher and
listing_quality_gate. Every 2026-07 category incident came from answering
"is this an outdoor product?" with a bare keyword hit, and each one had to be
fixed twice — the "balcony" false positive was patched in the matcher first and
live stayed wrong, because the audit runs the profile. One module now, so a fix
lands in both engines.
"""

from __future__ import annotations

import pytest

from src.utils.product_context_signals import (
    has_outdoor_marker,
    mentions_cooler_product,
    names_indoor_room,
    outdoor_context,
)


class TestOutdoorContext:
    INDOOR_COPY = (
        "For Living Room, Entryway, Dormitory, Bedroom. Use it as a temporary seat "
        "in the study, or a leisure bench on the balcony."
    )

    def test_incidental_balcony_does_not_make_an_indoor_product_outdoor(self):
        assert outdoor_context("Upholstered Storage Bench with Bench Daybed", self.INDOOR_COPY) is False

    def test_outdoor_word_in_title_still_wins(self):
        assert outdoor_context("Patio Rattan Daybed with Canopy", self.INDOOR_COPY) is True

    def test_outdoor_copy_without_indoor_rooms_counts(self):
        assert outdoor_context("Rattan Daybed", "Perfect for the patio and poolside.") is True

    def test_no_signal_at_all_is_false(self):
        assert outdoor_context("Wooden Coffee Table", "Solid oak construction.") is False

    def test_marker_and_room_helpers_are_independent(self):
        assert has_outdoor_marker("leisure bench on the balcony") is True
        assert names_indoor_room("For Living Room and Bedroom") is True
        assert names_indoor_room("Weather resistant wicker") is False


class TestCoolerIsSometimesAnAdjective:
    @pytest.mark.parametrize(
        "text",
        [
            "cozy atmosphere even in cooler weather",
            "ideal for cooler months on the patio",
            "comfortable in cooler temperatures",
            "breathable mesh promotes cooler air circulation",
            "great for cooler evenings outdoors",
        ],
    )
    def test_comparative_cooler_is_not_a_product(self, text):
        assert mentions_cooler_product(text) is False

    @pytest.mark.parametrize(
        "text",
        [
            "52QT Rotomolded Hard Cooler with Wheels",
            "Portable Cooler for camping trips",
            "Ice Chest holds 40 cans",
            "This cooler keeps ice for 5 days",
            "Insulated Cooler Bag, 30 can capacity",
        ],
    )
    def test_real_cooler_products_are_detected(self, text):
        assert mentions_cooler_product(text) is True

    def test_no_mention_at_all(self):
        assert mentions_cooler_product("Solid wood dining table") is False


class TestBothEnginesUseTheSharedSignals:
    """The point of the module: a guard fixed once must hold in both engines."""

    TITLE = (
        '65.75" Wide Modern Upholstered Storage Bench With Double Lids, '
        "Bench Daybed With Rubberwood Legs For Living Room, Entryway, Dormitory, Bedroom"
    )
    DESC = "Use it as a temporary seat in the study, or a leisure bench on the balcony."

    def test_profile_engine_does_not_call_it_outdoor(self):
        from src.utils.listing_quality_gate import classify_listing_profile

        assert classify_listing_profile(self.TITLE, self.DESC, "138996").kind != "outdoor_daybed"

    def test_matcher_engine_does_not_call_it_outdoor(self):
        from src.services.ebay_category_matcher import EbayCategoryMatcher

        class _Oauth:
            def get_application_token(self):
                return "offline"

        category_id, _ = EbayCategoryMatcher(_Oauth()).canonicalize_category(
            self.TITLE, "38204", "Benches", self.DESC
        )
        assert category_id != "138996"

    def test_matcher_engine_rejects_the_cooler_adjective(self):
        from src.services.ebay_category_matcher import EbayCategoryMatcher

        class _Oauth:
            def get_application_token(self):
                return "offline"

        category_id, _ = EbayCategoryMatcher(_Oauth()).canonicalize_category(
            "Luxury 4 Season Bell Tent with Stove Jack",
            "179010",
            "Tents",
            "Stay warm and cozy even in cooler weather.",
        )
        assert category_id != "79691"
