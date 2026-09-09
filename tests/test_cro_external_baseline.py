"""S38 \u2014 \u8de8\u5e73\u53f0\u57fa\u7ebf tests."""
from __future__ import annotations

from src.services.cro_external_baseline import (
    batch_classify, classify_signal,
)


def test_no_internal_drop_is_low_priority():
    r = classify_signal('A', 0.05, {'amazon': -0.20})
    assert r['verdict'] == 'no_internal_drop'
    assert r['priority'] == 'low'


def test_macro_drop_when_all_external_also_drop():
    r = classify_signal('B', -0.20, {'amazon': -0.15, 'walmart': -0.18})
    assert r['verdict'] == 'macro_drop'
    assert r['priority'] == 'low'


def test_platform_specific_when_external_up():
    r = classify_signal('C', -0.20, {'amazon': 0.10})
    assert r['verdict'] == 'platform_specific_drop'
    assert r['priority'] == 'high'


def test_no_external_baseline_when_dict_empty():
    r = classify_signal('D', -0.30, {})
    assert r['verdict'] == 'no_external_baseline'
    assert r['priority'] == 'medium'


def test_mixed_external_flat_is_likely_drop():
    r = classify_signal('E', -0.20, {'amazon': 0.0})
    assert r['verdict'] == 'platform_likely_drop'
    assert r['priority'] == 'medium'


def test_batch_classify_summary():
    rep = batch_classify([
        {'sku': 'A', 'internal_trend': -0.20, 'external_trends': {'amazon': 0.10}},
        {'sku': 'B', 'internal_trend': -0.20, 'external_trends': {'amazon': -0.20}},
        {'sku': 'C', 'internal_trend': 0.10},
    ])
    assert rep['evaluated'] == 3
    assert len(rep['high_priority']) == 1
    assert rep['macro_drop_skus'] == ['B']
