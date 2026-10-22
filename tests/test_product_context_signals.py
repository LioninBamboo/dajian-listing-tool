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
    has_compound_product_signal,
    has_outdoor_marker,
    mentions_cooler_product,
    names_indoor_room,
    outdoor_context,
    title_allows_outdoor_chair_remap,
    title_has_clear_outdoor_word,
    title_has_mixed_indoor_outdoor,
    title_is_chair_accessory,
    title_is_chair_only_dining,
    title_is_dining_set_bundle,
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

    def test_room_list_balcony_with_velvet_is_not_outdoor(self):
        title = "Floral Accent Chair Velvet Lounge Arm Chair for Living Room, Balcony"
        assert outdoor_context(title, title) is False

    def test_workbench_phrase_is_not_bench_with(self):
        from src.utils.product_context_signals import has_phrase, primary_product_noun

        assert has_phrase("Heavy Duty Steel Workbench with Pegboard", "bench with") is False
        assert primary_product_noun("Heavy Duty Steel Workbench with Pegboard") == "workbench"


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


class TestDiningSetBundleAndMixedOutdoor:
    def test_nulls_are_safe(self):
        assert title_is_dining_set_bundle(None) is False
        assert title_has_mixed_indoor_outdoor(None) is False
        assert title_has_clear_outdoor_word("") is False
        assert title_allows_outdoor_chair_remap(None) is False
        assert title_is_chair_accessory("") is False

    @pytest.mark.parametrize(
        "title",
        [
            "Outdoor Dining Table and Chairs",
            "Patio Dining Table and 4 Dining Chairs",
            "dining table with chairs",
            "7-Piece Patio Dining Set",
            "Bistro Table and 2 Chairs Set",
        ],
    )
    def test_table_plus_chairs_is_a_set_bundle(self, title):
        assert title_is_dining_set_bundle(title) is True
        assert has_compound_product_signal(title) is True

    def test_single_dining_chair_is_not_a_set(self):
        title = "Acacia Wood Outdoor Dining Chair, Rope Seat"
        assert title_is_dining_set_bundle(title) is False
        assert title_allows_outdoor_chair_remap(title) is True

    def test_mixed_indoor_outdoor_wording(self):
        assert title_has_mixed_indoor_outdoor("Round Dining Table Outdoor Indoor") is True
        assert title_has_clear_outdoor_word("Round Dining Table Outdoor Indoor") is False
        assert title_allows_outdoor_chair_remap("Indoor/Outdoor Folding Chairs Set of 4") is False

    def test_chair_cushions_are_accessories(self):
        assert title_is_chair_accessory("Patio Chair Cushions") is True
        assert title_allows_outdoor_chair_remap("Patio Chair Cushions") is False

    def test_chair_only_n_piece_is_not_a_table_set(self):
        assert title_is_chair_only_dining("4 Piece Outdoor Dining Chair Set") is True
        assert title_is_dining_set_bundle("4 Piece Outdoor Dining Chair Set") is False
        assert title_is_chair_only_dining("Outdoor 5-Piece Dining Chairs") is True
        assert title_is_dining_set_bundle("Outdoor 5-Piece Dining Chairs") is False
        assert title_is_dining_set_bundle("7-Piece Patio Dining Set") is True
        assert title_is_chair_only_dining("4 Piece Patio Dining Set with Chairs") is False

    def test_room_list_does_not_allow_outdoor_chair_remap(self):
        assert title_allows_outdoor_chair_remap(
            "Velvet Club Chair for Living Room, Patio, Garden"
        ) is False
        assert title_allows_outdoor_chair_remap(
            "Upholstered Dining Chair for Patio, Garden"
        ) is False
        assert title_allows_outdoor_chair_remap("Outdoor Dining Chair for Living Room") is True


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
