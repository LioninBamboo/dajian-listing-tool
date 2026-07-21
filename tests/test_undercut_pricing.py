"""Tests for undercut pricing + competitor analysis (B1, blind-box instance)."""

import dataclasses

import pytest

from src.utils.store_profile import StoreProfile
from src.services.undercut_pricing import (
    UndercutQuote,
    recommend_undercut_price,
    _to_99_at_or_below,
)


def _profile(**over):
    base = dataclasses.replace(
        StoreProfile(),
        pricing_strategy="undercut",
        undercut_pct=0.05,
        undercut_min_abs=0.5,
        price_ends_99=True,
    )
    return dataclasses.replace(base, **over) if over else base


class TestCore:
    def test_never_exceeds_source(self):
        q = recommend_undercut_price(24.50, profile=_profile())
        assert q.recommended_price < 24.50
        assert q.market_aware_price <= q.recommended_price

    def test_pct_and_min_abs_take_the_larger_cut(self):
        # 100 * (1-0.05) = 95 (pct cut = 5). min_abs=0.5 → 99.5. min() = 95.
        p = _profile(price_ends_99=False, undercut_pct=0.05, undercut_min_abs=0.5)
        q = recommend_undercut_price(100.0, profile=p)
        assert q.recommended_price == 95.0
        # Now make min_abs the deeper cut.
        p2 = _profile(price_ends_99=False, undercut_pct=0.01, undercut_min_abs=10.0)
        q2 = recommend_undercut_price(100.0, profile=p2)
        assert q2.recommended_price == 90.0

    def test_non_positive_source_raises(self):
        with pytest.raises(ValueError):
            recommend_undercut_price(0, profile=_profile())
        with pytest.raises(ValueError):
            recommend_undercut_price(-5, profile=_profile())

    def test_zero_undercut_still_never_above_source(self):
        p = _profile(price_ends_99=False, undercut_pct=0.0, undercut_min_abs=0.0)
        q = recommend_undercut_price(30.0, profile=p)
        assert q.recommended_price == 30.0


class TestCompetitorAnalysis:
    def test_source_above_market_median_offers_lower_alt(self):
        q = recommend_undercut_price(
            40.0, profile=_profile(), competitor_stats={"min": 18.0, "max": 55.0, "median": 25.0}
        )
        assert "source_above_market_median" in q.flags
        assert q.market_aware_price < q.recommended_price

    def test_below_market_min_is_flagged(self):
        q = recommend_undercut_price(
            15.0, profile=_profile(), competitor_stats={"min": 20.0, "max": 40.0, "median": 28.0}
        )
        assert "below_market_min" in q.flags

    def test_source_above_market_max_flagged(self):
        q = recommend_undercut_price(
            80.0, profile=_profile(), competitor_stats={"min": 20.0, "max": 55.0, "median": 30.0}
        )
        assert "source_above_market_max" in q.flags

    def test_no_stats_no_flags(self):
        q = recommend_undercut_price(24.0, profile=_profile())
        assert q.flags == []
        assert q.competitor_stats == {}

    def test_reasoning_is_human_readable(self):
        q = recommend_undercut_price(
            24.0, profile=_profile(), competitor_stats={"median": 22.0}
        )
        assert "source $24.00" in q.reasoning
        assert "undercut 5.0%" in q.reasoning


class TestPsychologicalRounding:
    @pytest.mark.parametrize(
        "x,expected",
        [(24.50, 23.99), (24.99, 24.99), (24.37, 23.99), (25.00, 24.99), (1.20, 0.99), (100.00, 99.99)],
    )
    def test_to_99_at_or_below(self, x, expected):
        assert _to_99_at_or_below(x) == expected

    def test_rounding_never_pushes_above_target(self):
        p = _profile(price_ends_99=True, undercut_pct=0.03, undercut_min_abs=0.0)
        q = recommend_undercut_price(24.50, profile=p)
        # 24.50*0.97 = 23.765 → .99 at or below = 23.99? no, floor 23, 23.99<=23.765? no →22.99
        assert q.recommended_price <= 23.765 + 1e-9


class TestTotalLanded:
    def test_undercuts_price_plus_shipping_not_price_alone(self):
        # competitor: $5 item + $9 shipping = $14 landed. Free-ship undercut must
        # be ~$14 total, NOT ~$5 (which ignoring shipping would produce).
        p = _profile(price_ends_99=False, undercut_pct=0.05, undercut_min_abs=0.0)
        q = recommend_undercut_price(5.0, source_shipping=9.0, profile=p)
        assert q.source_total == 14.0
        assert q.recommended_total == pytest.approx(14.0 * 0.95, abs=0.01)  # ~13.30
        assert q.recommended_total > 5.0  # would-be bug: undercutting price alone

    def test_free_model_puts_all_in_item_price(self):
        p = _profile(price_ends_99=False, shipping_model="free", undercut_pct=0.0, undercut_min_abs=0.0)
        q = recommend_undercut_price(20.0, source_shipping=8.0, profile=p)
        assert q.recommended_shipping == 0.0
        assert q.recommended_price == 28.0
        assert q.recommended_total == 28.0
        assert q.shipping_model == "free"

    def test_fixed_model_splits_item_and_shipping(self):
        p = _profile(price_ends_99=False, shipping_model="fixed", fixed_shipping_amount=8.99,
                     undercut_pct=0.0, undercut_min_abs=0.0)
        q = recommend_undercut_price(20.0, source_shipping=8.0, profile=p)
        assert q.recommended_shipping == 8.99
        assert q.recommended_price == pytest.approx(28.0 - 8.99, abs=0.01)
        assert q.recommended_total == pytest.approx(28.0, abs=0.01)
        assert q.shipping_model == "fixed"

    def test_no_shipping_matches_legacy_price_only_behavior(self):
        p = _profile(price_ends_99=False, undercut_pct=0.05, undercut_min_abs=0.0)
        q = recommend_undercut_price(100.0, profile=p)  # no source_shipping
        assert q.source_shipping == 0.0
        assert q.recommended_price == 95.0
        assert q.recommended_total == 95.0

    def test_fixed_shipping_never_makes_item_price_nonpositive(self):
        p = _profile(price_ends_99=False, shipping_model="fixed", fixed_shipping_amount=8.99,
                     undercut_pct=0.0, undercut_min_abs=0.0)
        q = recommend_undercut_price(3.0, source_shipping=0.0, profile=p)  # total 3.0 < 8.99
        assert q.recommended_price >= 0.01
        assert q.recommended_shipping <= q.recommended_total


class TestSerialization:
    def test_to_dict_round_trips_fields(self):
        q = recommend_undercut_price(24.0, source_shipping=6.0, profile=_profile())
        d = q.to_dict()
        assert set(d) == {
            "source_price",
            "source_shipping",
            "source_total",
            "recommended_price",
            "recommended_shipping",
            "recommended_total",
            "market_aware_price",
            "shipping_model",
            "competitor_stats",
            "flags",
            "reasoning",
        }
        assert isinstance(q, UndercutQuote)
