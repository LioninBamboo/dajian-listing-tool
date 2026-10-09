"""S72 — inventory throttle + forecast tests."""
from __future__ import annotations

from src.services.cro_inventory_throttle_forecast import (
    evaluate_with_forecast, is_throttled_forecast,
)


def _const_history(daily, days=14):
    return [daily] * days


def test_constant_demand_below_lead_time_triggers_throttle():
    # 5 units stock, 2/day → 3 days runway < 7-day lead
    out = evaluate_with_forecast(
        [{'sku': 'A', 'stock': 5, 'history': _const_history(2)}],
        lead_time_days=7,
    )
    assert 'A' in out['throttle_skus']
    d = out['details'][0]
    assert d['days_to_stockout'] == 3
    assert d['throttled'] is True


def test_constant_demand_above_lead_time_no_throttle():
    # 100 units, 2/day → 50 days runway >> 7-day lead
    out = evaluate_with_forecast(
        [{'sku': 'A', 'stock': 100, 'history': _const_history(2)}],
        lead_time_days=7,
    )
    assert out['throttle_skus'] == []
    assert out['details'][0]['throttled'] is False


def test_zero_demand_never_throttled():
    out = evaluate_with_forecast(
        [{'sku': 'A', 'stock': 0, 'history': _const_history(0)}],
    )
    assert out['throttle_skus'] == []


def test_short_history_falls_back_to_average():
    out = evaluate_with_forecast(
        [{'sku': 'A', 'stock': 3, 'history': [1, 1, 1]}],  # <7 days
        lead_time_days=7,
    )
    assert out['details'][0]['avg_daily_forecast'] == 1.0
    # 3 units / 1 per day = 3 days < 7 lead → throttle
    assert 'A' in out['throttle_skus']


def test_skips_rows_missing_sku():
    out = evaluate_with_forecast([
        {'stock': 5, 'history': [1] * 14},
        {'sku': 'B', 'stock': 5, 'history': [1] * 14},
    ])
    assert out['total'] == 1
    assert out['details'][0]['sku'] == 'B'


def test_is_throttled_forecast_helper():
    rep = {'throttle_skus': ['A', 'B']}
    assert is_throttled_forecast('A', rep)
    assert not is_throttled_forecast('C', rep)


def test_growing_demand_triggers_earlier():
    # 50 units, demand growing → days_to_stockout shrinks
    growing = list(range(1, 15))  # 1,2,...,14
    out = evaluate_with_forecast(
        [{'sku': 'A', 'stock': 50, 'history': growing}],
        lead_time_days=7, horizon=14,
    )
    d = out['details'][0]
    assert d['avg_daily_forecast'] > 14  # 趋势外推大于最后值
