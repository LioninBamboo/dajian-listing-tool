"""Guards for sales_health_check auto-fix price cuts.

Reproduces the W3118P505149 under-cost incident:
when total_dajian_cost is missing, auto-fix must NOT follow a bad
market average through a zero floor.
"""
from __future__ import annotations

import pytest

from scripts import sales_health_check as shc
from src.services.pricing_engine import PricingEngine


# ── resolve_total_cost ─────────────────────────────────────────────────────

def test_resolve_total_cost_prefers_stored_total_dajian_cost():
    assert shc.resolve_total_cost(
        {"total_dajian_cost": 160.11, "product_price": 1, "shipping_cost": 1},
        product_price=999,
        shipping_cost=999,
    ) == pytest.approx(160.11)


def test_resolve_total_cost_recomputes_from_collect_price_when_missing():
    expected = PricingEngine.calculate_dajian_cost(115.0, 35.94)["total_dajian_cost"]
    got = shc.resolve_total_cost(
        {"selling_price": 62.41, "health_repriced_at": "2026-08-10"},
        product_price=115.0,
        shipping_cost=35.94,
    )
    assert got == pytest.approx(expected)
    assert got > 150  # must be full landed cost, not bare product price


def test_resolve_total_cost_returns_zero_when_no_data():
    assert shc.resolve_total_cost({}, product_price=0, shipping_cost=0) == 0.0
    assert shc.resolve_total_cost(None) == 0.0


# ── health_price_floor ─────────────────────────────────────────────────────

def test_health_price_floor_none_when_cost_missing():
    assert shc.health_price_floor(0) is None
    assert shc.health_price_floor(None) is None
    assert shc.health_price_floor(-1) is None


def test_health_price_floor_matches_pricing_engine_safe_floor():
    total_cost = 160.11
    floor = shc.health_price_floor(total_cost, min_margin=0.10)
    assert floor == pytest.approx(
        PricingEngine.safe_floor_price(total_cost, 0.10), abs=0.02
    )
    assert floor > total_cost  # listing price must exceed landed cost


# ── plan_auto_reprice (the W3118 regression) ───────────────────────────────

def test_overpriced_plan_skips_when_cost_unknown_even_if_market_low():
    """The exact bug: market $65.69, current $89.47, total_cost=0 → used to cut to $62.41."""
    plan = shc.plan_auto_reprice(
        current_price=89.47,
        total_cost=0,
        market_avg=65.69,
        mode="overpriced",
    )
    assert plan is None


def test_overpriced_plan_skips_when_only_collect_price_missing_and_cost_zero():
    plan = shc.plan_auto_reprice(
        current_price=115.0,
        total_cost=0.0,
        market_avg=65.69,
        mode="overpriced",
    )
    assert plan is None


def test_overpriced_plan_uses_floor_not_market_dump_when_market_below_cost():
    """With real cost, market_avg*0.95 below floor → stay at floor; if floor >= current, no cut."""
    total_cost = PricingEngine.calculate_dajian_cost(115.0, 35.94)["total_dajian_cost"]
    floor = PricingEngine.safe_floor_price(total_cost, 0.10)
    # Current already below floor (broken listing) — auto-fix must not cut further.
    plan = shc.plan_auto_reprice(
        current_price=89.47,
        total_cost=total_cost,
        market_avg=65.69,
        mode="overpriced",
    )
    assert plan is None  # would raise to floor, but health auto-fix is cut-only
    assert floor > 89.47


def test_overpriced_plan_allows_cut_when_above_floor():
    total_cost = 100.0
    floor = PricingEngine.safe_floor_price(total_cost, 0.10)
    current = floor * 1.40  # clearly above floor
    # Market below current so listing is "overpriced"; target=market*0.95 still above floor.
    market = current / 1.25  # current is 25% above market
    plan = shc.plan_auto_reprice(
        current_price=current,
        total_cost=total_cost,
        market_avg=market,
        mode="overpriced",
    )
    assert plan is not None
    assert plan["new_price"] >= floor - 0.01
    assert plan["new_price"] < current * 0.97
    assert plan["reason"] == "定价偏高 → 跟价"


def test_low_conversion_plan_skips_without_cost():
    plan = shc.plan_auto_reprice(
        current_price=200.0,
        total_cost=0,
        mode="low_conversion",
    )
    assert plan is None


def test_low_conversion_plan_respects_floor():
    total_cost = 100.0
    floor = PricingEngine.safe_floor_price(total_cost, 0.10)
    # 5% cut would go below floor — clamp to floor; if still not a real cut, skip
    current = floor * 1.02  # only 2% above floor
    plan = shc.plan_auto_reprice(
        current_price=current,
        total_cost=total_cost,
        mode="low_conversion",
    )
    # 5% cut from current would be ~0.97*current which may be below floor
    # After clamp to floor, drop vs current is only ~2% → should skip (<1% threshold fails? or 1%)
    # low_conversion requires new < current * 0.99
    if plan is not None:
        assert plan["new_price"] >= floor - 0.01
        assert plan["new_price"] < current


def test_low_conversion_plan_cuts_five_percent_when_safe():
    total_cost = 50.0
    floor = PricingEngine.safe_floor_price(total_cost, 0.10)
    current = max(floor * 1.5, 200.0)
    plan = shc.plan_auto_reprice(
        current_price=current,
        total_cost=total_cost,
        mode="low_conversion",
    )
    assert plan is not None
    assert plan["new_price"] == pytest.approx(round(current * 0.95, 2))
    assert plan["new_price"] >= floor
    assert plan["reason"] == "低转化 → 降价5%"


# ── loss_making plan ────────────────────────────────────────────────────────

def test_loss_making_plan_raises_listing_to_safe_floor():
    """A live under-cost listing must be raised, not merely reported."""
    total_cost = 196.59
    floor = PricingEngine.safe_floor_price(total_cost, 0.10)
    plan = shc.plan_auto_reprice(
        current_price=248.45,
        total_cost=total_cost,
        mode="loss_making",
    )
    assert plan is not None
    assert plan["new_price"] == pytest.approx(floor, abs=0.01)
    assert plan["new_price"] > 248.45
    assert plan["reason"] == "潜在亏损 → 提升至安全底价"


# ── evaluate_live_loss (why daily report never warned) ─────────────────────

def test_evaluate_live_loss_flags_w3118_style_under_cost():
    """$62.41 live vs ~$160 cost must be loss_making — even if suggested_price is 0."""
    total_cost = PricingEngine.calculate_dajian_cost(115.0, 35.94)["total_dajian_cost"]
    loss = shc.evaluate_live_loss(62.41, total_cost)
    assert loss is not None
    assert loss["ebay_price"] == 62.41
    assert loss["total_cost"] == pytest.approx(total_cost, abs=0.02)
    assert loss["loss"] > 90  # severe under-cost


def test_evaluate_live_loss_none_when_price_covers_cost():
    total_cost = PricingEngine.calculate_dajian_cost(115.0, 35.94)["total_dajian_cost"]
    assert shc.evaluate_live_loss(248.67, total_cost) is None
    assert shc.evaluate_live_loss(227.16, total_cost) is None


def test_evaluate_live_loss_none_when_cost_or_price_missing():
    assert shc.evaluate_live_loss(62.41, 0) is None
    assert shc.evaluate_live_loss(0, 160.11) is None
