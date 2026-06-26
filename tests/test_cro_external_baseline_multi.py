"""S68 — multi-platform baseline tests."""
from __future__ import annotations

from src.services.cro_external_baseline_multi import (
    batch_classify_multi, classify_multi,
)


def test_no_internal_drop_short_circuits():
    out = classify_multi('A', internal_trend=0.02,
                         platforms={'amazon': -0.20, 'walmart': -0.15})
    assert out['verdict'] == 'no_internal_drop'
    assert out['priority'] == 'low'


def test_no_external_baseline():
    out = classify_multi('A', internal_trend=-0.20, platforms={})
    assert out['verdict'] == 'no_external_baseline'


def test_consensus_drop_two_platforms():
    out = classify_multi('A', internal_trend=-0.20,
                         platforms={'amazon': -0.15, 'walmart': -0.12})
    assert out['verdict'] == 'consensus_drop'
    assert out['priority'] == 'low'
    assert set(out['drop_platforms']) == {'amazon', 'walmart'}


def test_platform_specific_drop_when_others_rising():
    out = classify_multi('A', internal_trend=-0.20,
                         platforms={'amazon': 0.10, 'walmart': 0.08})
    assert out['verdict'] == 'platform_specific_drop'
    assert out['priority'] == 'high'


def test_divergent_drop_mixed_signals():
    out = classify_multi('A', internal_trend=-0.20,
                         platforms={'amazon': -0.15, 'walmart': 0.10})
    assert out['verdict'] == 'divergent_drop'
    assert out['priority'] == 'medium'


def test_partial_drop_one_platform():
    out = classify_multi('A', internal_trend=-0.20,
                         platforms={'amazon': -0.20, 'walmart': 0.0})
    assert out['verdict'] == 'partial_drop'
    assert out['priority'] == 'medium'


def test_platform_likely_drop_all_flat():
    out = classify_multi('A', internal_trend=-0.20,
                         platforms={'amazon': 0.0, 'walmart': 0.01})
    assert out['verdict'] == 'platform_likely_drop'


def test_unknown_trend_handled():
    out = classify_multi('A', internal_trend=-0.20,
                         platforms={'amazon': None, 'walmart': -0.20})
    # only walmart counted as drop → partial_drop
    assert out['verdict'] == 'partial_drop'


def test_batch_classify_multi_aggregates():
    records = [
        {'sku': 'A', 'internal_trend': -0.20,
         'platforms': {'amazon': -0.15, 'walmart': -0.12}},
        {'sku': 'B', 'internal_trend': -0.20,
         'platforms': {'amazon': 0.10, 'walmart': 0.08}},
        {'sku': 'C', 'internal_trend': 0.05, 'platforms': {}},
    ]
    out = batch_classify_multi(records)
    assert out['total'] == 3
    assert 'A' in out['consensus_drop_skus']
    assert out['high_priority_skus'] == ['B']
    assert 'C' in out['by_verdict']['no_internal_drop']
