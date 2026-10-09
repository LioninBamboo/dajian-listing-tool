"""S98 — promo uplift tests."""
from __future__ import annotations

from src.services.cro_promo_uplift_attribution import (
    batch_split_uplift, estimate_baseline_daily, split_uplift,
)


def test_estimate_baseline_simple():
    assert estimate_baseline_daily([1, 2, 3, 4, 5]) == 3.0


def test_estimate_baseline_window():
    assert estimate_baseline_daily(list(range(20)), window_days=5) == 17.0


def test_estimate_baseline_empty():
    assert estimate_baseline_daily([]) == 0.0


def test_split_uplift_basic_positive_lift():
    out = split_uplift(baseline_daily=10, promoted_period_sales=200,
                       promoted_days=10)
    # organic_expected = 100, uplift = 100, pct = 1.0
    assert out['uplift_units'] == 100
    assert out['uplift_pct'] == 1.0
    assert out['is_incremental_positive']


def test_split_uplift_negative_lift():
    out = split_uplift(10, 50, 10)
    assert out['uplift_units'] == -50
    assert not out['is_incremental_positive']


def test_split_uplift_invalid_days():
    out = split_uplift(10, 100, 0)
    assert out['error'] == 'invalid_promoted_days'


def test_split_uplift_iroi_when_ad_spend_set():
    out = split_uplift(10, 200, 10, ad_spend=50, unit_margin=2)
    # incremental units=100, incremental margin = 200, iroi = 200/50 = 4.0
    assert out['incremental_margin_value'] == 200
    assert out['incremental_roi'] == 4.0


def test_split_uplift_iroi_none_when_no_ad_spend():
    out = split_uplift(10, 200, 10, unit_margin=2)
    assert out['incremental_roi'] is None


def test_split_uplift_pct_none_when_no_organic():
    out = split_uplift(0, 50, 5)
    assert out['organic_expected'] == 0
    assert out['uplift_pct'] is None


def test_batch_split_uplift_classifies_profitable_and_losers():
    rows = [
        {'sku': 'A', 'history': [10] * 7, 'promoted_sales': 200,
         'promoted_days': 10, 'ad_spend': 50, 'unit_margin': 2},  # ROI 4
        {'sku': 'B', 'history': [10] * 7, 'promoted_sales': 110,
         'promoted_days': 10, 'ad_spend': 100, 'unit_margin': 2},  # ROI 0.2
    ]
    out = batch_split_uplift(rows)
    assert out['count'] == 2
    assert out['profitable_count'] == 1
    assert out['loser_count'] == 1
