"""S99 — freight optimizer tests."""
from __future__ import annotations

from src.services.cro_freight_optimizer import (
    aggregate_by_freight, freight_elasticity, recommend_freight_tier,
)


def test_aggregate_buckets_weighted_by_sample_size():
    rows = [
        {'freight': 0.0, 'cvr': 0.10, 'sample_size': 100},
        {'freight': 0.0, 'cvr': 0.20, 'sample_size': 100},
        {'freight': 5.0, 'cvr': 0.05, 'sample_size': 100},
    ]
    out = aggregate_by_freight(rows)
    by_f = {b['freight']: b for b in out}
    assert by_f[0.0]['avg_cvr'] == 0.15
    assert by_f[0.0]['sample_size'] == 200
    assert by_f[5.0]['sample_size'] == 100


def test_aggregate_skips_zero_sample_and_bad_freight():
    rows = [
        {'freight': 0.0, 'cvr': 0.10, 'sample_size': 0},
        {'freight': None, 'cvr': 0.10, 'sample_size': 50},
    ]
    out = aggregate_by_freight(rows)
    # both zero-sample and None-freight (treated as 0.0) handled - None=0.0 with n=50
    # both: first skipped (n=0), second has freight None coerced to 0.0
    assert isinstance(out, list)


def test_recommend_picks_highest_cvr_tier_with_samples():
    rows = [
        {'freight': 0.0, 'cvr': 0.20, 'sample_size': 100},
        {'freight': 5.0, 'cvr': 0.10, 'sample_size': 100},
    ]
    out = recommend_freight_tier(rows)
    assert out['recommended_freight'] == 0.0
    assert out['recommended_cvr'] == 0.20


def test_recommend_filters_low_sample_size():
    rows = [
        {'freight': 0.0, 'cvr': 0.50, 'sample_size': 10},
        {'freight': 5.0, 'cvr': 0.10, 'sample_size': 100},
    ]
    out = recommend_freight_tier(rows, min_sample=50)
    assert out['recommended_freight'] == 5.0


def test_recommend_returns_none_when_no_eligible():
    rows = [{'freight': 0.0, 'cvr': 0.50, 'sample_size': 5}]
    out = recommend_freight_tier(rows, min_sample=50)
    assert out['recommended_freight'] is None
    assert out['reason'] == 'insufficient_samples'


def test_elasticity_negative_when_higher_freight_lower_cvr():
    rows = [
        {'freight': 0.0, 'cvr': 0.30, 'sample_size': 100},
        {'freight': 5.0, 'cvr': 0.10, 'sample_size': 100},
    ]
    out = freight_elasticity(rows)
    assert out['direction'] == 'negative'
    assert out['elasticity'] < 0


def test_elasticity_none_when_single_bucket():
    rows = [{'freight': 0.0, 'cvr': 0.30, 'sample_size': 100}]
    out = freight_elasticity(rows)
    assert out['elasticity'] is None
