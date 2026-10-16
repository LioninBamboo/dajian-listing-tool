"""S113 — pricing psychology tests."""
from __future__ import annotations

from src.services.cro_pricing_psychology import (
    batch_align, candidate_charm_prices, is_charm_price,
    recommend_charm_price,
)


def test_is_charm_price_true():
    assert is_charm_price(9.99) is True
    assert is_charm_price(19.95) is True
    assert is_charm_price(7.49) is True


def test_is_charm_price_false():
    assert is_charm_price(10.00) is False
    assert is_charm_price(15.50) is False
    assert is_charm_price(0) is False


def test_candidate_unique_and_includes_99():
    cs = candidate_charm_prices(10.10)
    assert 9.99 in cs or 10.99 in cs


def test_recommend_picks_charm_close_to_target():
    out = recommend_charm_price(10.05, max_deviation_pct=0.10)
    assert out['recommended'] == 9.99


def test_recommend_respects_min_price():
    out = recommend_charm_price(10.05, min_price=10.00,
                                  max_deviation_pct=0.20)
    assert out['recommended'] is not None
    assert out['recommended'] >= 10.00


def test_recommend_respects_max_price():
    out = recommend_charm_price(10.05, max_price=10.00,
                                  max_deviation_pct=0.20)
    assert out['recommended'] is not None
    assert out['recommended'] <= 10.00


def test_recommend_no_candidate_when_too_strict():
    out = recommend_charm_price(10.05, min_price=10.50, max_price=10.60,
                                  max_deviation_pct=0.001)
    assert out['recommended'] is None
    assert out['reason'] == 'no_candidate_in_range'


def test_recommend_invalid_target():
    out = recommend_charm_price(0)
    assert out['recommended'] is None


def test_batch_align_counts():
    rows = [
        {'sku': 'A', 'target_price': 10.05},
        {'sku': 'B', 'target_price': 0},
        {'sku': 'C', 'target_price': 19.95},
    ]
    out = batch_align(rows)
    assert out['count'] == 3
    assert out['aligned_count'] == 2
