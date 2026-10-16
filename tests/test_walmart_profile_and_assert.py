"""P15 — Walmart 品类 fee + 跨平台 assert_safe_price tests."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.services.platform_fee_profile import (
    walmart_profile_for_category, assert_safe_price,
    EBAY_PROFILE, WALMART_FEE_BY_CATEGORY,
)


def test_walmart_jewelry_higher_fee_than_default():
    j = walmart_profile_for_category('jewelry')
    d = walmart_profile_for_category('unknown_xxx')
    assert j.fee_rate > d.fee_rate
    assert j.fee_rate == Decimal('0.20')
    assert d.fee_rate == WALMART_FEE_BY_CATEGORY['default']


def test_walmart_electronics_floor_lower_than_apparel():
    e = walmart_profile_for_category('electronics')
    a = walmart_profile_for_category('apparel')
    # 电子 fee 8% < 服装 15% → 死线低
    assert e.absolute_floor(20, ad_rate=0) < a.absolute_floor(20, ad_rate=0)


def test_assert_safe_price_passes():
    # eBay cost=20, price 远高于安全底
    assert_safe_price(price=50, cost=20, profile_name='ebay', safety_margin=0.05)


def test_assert_safe_price_raises_on_underwater():
    with pytest.raises(ValueError, match='safe_floor'):
        assert_safe_price(price=20, cost=20, profile_name='ebay', safety_margin=0.05)


def test_assert_safe_price_walmart_no_fixed_fee():
    # Walmart electronics 8% fee, 20 成本 + 5% 安全; 价格 25 应该足够
    assert_safe_price(price=30, cost=20, profile_name='walmart',
                      safety_margin=0.05, ad_rate=0)


def test_assert_safe_price_unknown_profile_raises_key_error():
    with pytest.raises(KeyError):
        assert_safe_price(price=50, cost=20, profile_name='shopee')


def test_walmart_category_profile_name_includes_category():
    p = walmart_profile_for_category('home_garden')
    assert 'home_garden' in p.name
