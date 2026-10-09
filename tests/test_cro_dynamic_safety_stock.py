"""S103 — dynamic safety stock tests."""
from __future__ import annotations

from src.services.cro_dynamic_safety_stock import (
    batch_compute, compute_safety_stock, cv, recommend_service_level,
)


def test_cv_zero_mean():
    assert cv([0, 0, 0]) == 0.0


def test_cv_basic():
    out = cv([10, 10, 10])  # std=0 → cv=0
    assert out == 0.0


def test_cv_positive():
    out = cv([5, 10, 15])
    assert out > 0


def test_recommend_high_service_level_for_high_variance():
    assert recommend_service_level(1.5) == 0.99
    assert recommend_service_level(0.7) == 0.97
    assert recommend_service_level(0.4) == 0.95
    assert recommend_service_level(0.1) == 0.90


def test_compute_insufficient_data():
    out = compute_safety_stock([], 14)
    assert out['safety_stock'] == 0
    assert out['reason'] == 'insufficient_data'


def test_compute_zero_lead_time():
    out = compute_safety_stock([5, 5, 5], 0)
    assert out['safety_stock'] == 0


def test_compute_constant_history_zero_safety():
    out = compute_safety_stock([10, 10, 10, 10], 14)
    assert out['safety_stock'] == 0
    assert out['reorder_point'] == 140  # mean*lead


def test_compute_variable_history_positive_safety():
    out = compute_safety_stock([5, 10, 15, 20, 25], 14)
    assert out['safety_stock'] > 0
    assert out['reorder_point'] > out['mean_daily'] * 14


def test_compute_uses_recommended_service_level_when_none():
    out = compute_safety_stock([5, 10, 15, 20, 25], 14)
    assert out['service_level'] in (0.90, 0.95, 0.97, 0.99)


def test_compute_respects_explicit_service_level():
    out = compute_safety_stock([5, 10, 15, 20, 25], 14, service_level=0.99)
    assert out['service_level'] == 0.99
    assert out['z'] == 2.33


def test_batch_compute_flags_reorder():
    rows = [
        {'sku': 'A', 'history': [10] * 7, 'lead_time_days': 7, 'on_hand': 5},
        {'sku': 'B', 'history': [10] * 7, 'lead_time_days': 7, 'on_hand': 1000},
    ]
    out = batch_compute(rows)
    assert out['count'] == 2
    assert out['needs_reorder_count'] == 1
    flagged = [i for i in out['items'] if i['needs_reorder']]
    assert flagged[0]['sku'] == 'A'
