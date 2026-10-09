"""S106 — lifecycle stage tests."""
from __future__ import annotations

from src.services.cro_lifecycle_stage import (
    batch_classify, classify_lifecycle,
)


def test_new_when_age_under_14():
    out = classify_lifecycle({'age_days': 5, 'recent_sales': [0]*5})
    assert out['stage'] == 'new'


def test_stagnant_when_old_and_no_sales():
    out = classify_lifecycle({'age_days': 60, 'recent_sales': [0]*30})
    assert out['stage'] == 'stagnant'


def test_growth_when_slope_positive():
    out = classify_lifecycle({'age_days': 30,
                              'recent_sales': [1, 2, 3, 4, 5, 6]})
    assert out['stage'] == 'growth'


def test_decline_when_slope_negative():
    out = classify_lifecycle({'age_days': 100,
                              'recent_sales': [10, 8, 6, 4, 2]})
    assert out['stage'] == 'decline'


def test_mature_when_flat_and_old():
    out = classify_lifecycle({'age_days': 100,
                              'recent_sales': [3, 3, 3, 3, 3]})
    assert out['stage'] == 'mature'


def test_mature_when_growth_but_too_old():
    # age >=90 + 上升 → 不算 growth, 落到 mature
    out = classify_lifecycle({'age_days': 120,
                              'recent_sales': [1, 2, 3, 4, 5, 6]})
    assert out['stage'] == 'mature'


def test_recommendation_present():
    out = classify_lifecycle({'age_days': 5})
    assert out['recommendation']
    assert isinstance(out['recommendation'], str)


def test_batch_classify_aggregates_by_stage():
    rows = [
        {'sku': 'A', 'age_days': 5},                                      # new
        {'sku': 'B', 'age_days': 60, 'recent_sales': [0]*30},             # stagnant
        {'sku': 'C', 'age_days': 100, 'recent_sales': [3]*5},             # mature
    ]
    out = batch_classify(rows)
    assert out['count'] == 3
    assert out['by_stage'].get('new', 0) == 1
    assert out['by_stage'].get('stagnant', 0) == 1
    assert out['by_stage'].get('mature', 0) == 1
