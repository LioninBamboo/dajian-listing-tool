"""P11 — platform_fee_profile tests.

关键: EbayFeeProfile 必须与 PricingEngine 100% 一致 (Decimal exact).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.services.platform_fee_profile import (
    EBAY_PROFILE, get_profile, register_profile, PlatformFeeProfile,
)
from src.services.pricing_engine import PricingEngine


@pytest.mark.parametrize("ad_rate", [None, 0, 0.03, 0.05, 0.08])
def test_ebay_denom_matches_pricing_engine(ad_rate):
    a = EBAY_PROFILE.discount_denom(ad_rate)
    b = PricingEngine._discount_denom(ad_rate)
    assert a == b, f"denom mismatch at ad_rate={ad_rate}: {a} vs {b}"


@pytest.mark.parametrize("cost", [10, 20.5, 99.99, 200])
@pytest.mark.parametrize("ad_rate", [None, 0, 0.05])
def test_ebay_floor_matches_pricing_engine(cost, ad_rate):
    a = EBAY_PROFILE.absolute_floor(cost, ad_rate=ad_rate)
    b = PricingEngine.absolute_floor_price(Decimal(str(cost)), ad_rate=ad_rate)
    assert float(a) == pytest.approx(float(b)), f"floor mismatch cost={cost} ad={ad_rate}: {a} vs {b}"


def test_invalid_ad_rate_raises():
    with pytest.raises(ValueError):
        EBAY_PROFILE.discount_denom(ad_rate=-0.01)
    with pytest.raises(ValueError):
        EBAY_PROFILE.discount_denom(ad_rate=0.95)


def test_required_ad_rate_positive_when_safe():
    # cost=20, price=50: 应能撑得起较大广告
    r = EBAY_PROFILE.required_ad_rate(price=50, cost=20, safety_margin=0.05)
    assert r > Decimal('0.05')


def test_required_ad_rate_negative_when_unprofitable():
    r = EBAY_PROFILE.required_ad_rate(price=10, cost=10, safety_margin=0.05)
    assert r < 0


def test_register_and_get_custom_profile():
    custom = PlatformFeeProfile(
        name='tiktok', fee_rate=Decimal('0.08'),
        default_ad_rate=Decimal('0'), fixed_fee=Decimal('0'),
    )
    register_profile('tiktok', custom)
    assert get_profile('TikTok').name == 'tiktok'


def test_unknown_profile_raises():
    with pytest.raises(KeyError):
        get_profile('shopee')


def test_walmart_profile_no_fixed_fee():
    w = get_profile('walmart')
    assert w.fixed_fee == Decimal('0')
    # cost=20, no fixed, lower discount → floor < eBay floor
    floor = w.absolute_floor(20, ad_rate=0)
    assert floor < EBAY_PROFILE.absolute_floor(20, ad_rate=0)
