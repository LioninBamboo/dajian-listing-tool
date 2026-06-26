"""S86 — replenish recommend tests."""
from __future__ import annotations

from src.services.cro_replenish_recommend import (
    batch_recommend, recommend_replenish,
)


def test_recommend_basic_sufficient_history():
    history = [10, 12, 9, 11, 10, 13, 12, 11, 10, 12]
    out = recommend_replenish('SKU1', history, on_hand=20,
                              lead_time_days=7, safety_days=3)
    assert out['horizon_days'] == 10
    # avg ~ 11, need ~ 110, on_hand 20 → 90+/-
    assert out['recommend_qty'] >= 80
    assert out['avg_daily'] > 0


def test_recommend_short_history_uses_average():
    history = [5, 5, 5]  # < MIN_HISTORY=7
    out = recommend_replenish('SKU2', history, on_hand=0,
                              lead_time_days=7, safety_days=3)
    assert out['recommend_qty'] == 50  # 5*10


def test_recommend_zero_history_zero_qty():
    out = recommend_replenish('SKU3', [], on_hand=10)
    assert out['recommend_qty'] == 0
    assert out['days_runway'] is None
    assert out['urgency'] == 'none'


def test_recommend_on_order_reduces_qty():
    history = [10] * 10
    out_no_order = recommend_replenish('A', history, on_hand=20)
    out_with_order = recommend_replenish('A', history, on_hand=20,
                                          on_order=50)
    assert out_with_order['recommend_qty'] < out_no_order['recommend_qty']


def test_recommend_qty_clamped_zero_when_overstocked():
    history = [1] * 10
    out = recommend_replenish('A', history, on_hand=1000)
    assert out['recommend_qty'] == 0


def test_urgency_critical_when_zero_stock():
    out = recommend_replenish('A', [10] * 10, on_hand=0)
    assert out['urgency'] == 'critical'


def test_urgency_high_when_runway_within_lead_time():
    # avg 10, on_hand 50 → runway=5, lead_time=7 → high
    out = recommend_replenish('A', [10] * 10, on_hand=50, lead_time_days=7)
    assert out['urgency'] == 'high'


def test_urgency_low_when_runway_far():
    out = recommend_replenish('A', [10] * 10, on_hand=500, lead_time_days=7)
    assert out['urgency'] == 'low'


def test_batch_recommend_aggregates():
    rows = [
        {'sku': 'A', 'history': [10] * 10, 'on_hand': 0},     # critical
        {'sku': 'B', 'history': [5] * 10, 'on_hand': 1000},   # none/low
        {'sku': 'C', 'history': [8] * 10, 'on_hand': 50},     # high
    ]
    out = batch_recommend(rows)
    assert len(out['items']) == 3
    assert out['critical_count'] == 1
    assert out['needs_replenish_count'] >= 2
    assert out['total_units_to_order'] > 0
