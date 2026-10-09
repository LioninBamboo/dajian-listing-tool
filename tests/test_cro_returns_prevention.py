"""S111 — returns prevention tests."""
from __future__ import annotations

from src.services.cro_returns_prevention import batch_evaluate, evaluate_sku


def test_low_volume_keeps_even_with_high_rate():
    out = evaluate_sku({'sku': 'A', 'sold_30d': 5, 'returned_30d': 3})
    assert out['decision'] == 'keep'


def test_high_rate_with_volume_pause():
    out = evaluate_sku({'sku': 'A', 'sold_30d': 100, 'returned_30d': 30,
                         'reason_counts': {'QUALITY': 25, 'SIZE': 5}})
    assert out['decision'] == 'pause'
    assert out['dominant_reason'] == 'quality'
    assert '整改' in out['fix_hint']


def test_medium_rate_review():
    out = evaluate_sku({'sku': 'A', 'sold_30d': 100, 'returned_30d': 12,
                         'reason_counts': {'SIZE': 12}})
    assert out['decision'] == 'review'
    assert out['dominant_reason'] == 'size'
    assert '尺寸' in out['fix_hint'] or '尺码' in out['fix_hint']


def test_low_rate_keep():
    out = evaluate_sku({'sku': 'A', 'sold_30d': 100, 'returned_30d': 2})
    assert out['decision'] == 'keep'


def test_zero_sold_not_crash():
    out = evaluate_sku({'sku': 'A', 'sold_30d': 0, 'returned_30d': 0})
    assert out['decision'] == 'keep'
    assert out['return_rate'] == 0.0


def test_size_dominant_when_more_size_than_quality():
    out = evaluate_sku({'sku': 'A', 'sold_30d': 100, 'returned_30d': 25,
                         'reason_counts': {'WRONG_SIZE': 20, 'QUALITY': 5}})
    assert out['dominant_reason'] == 'size'


def test_unknown_reason_fallback():
    out = evaluate_sku({'sku': 'A', 'sold_30d': 100, 'returned_30d': 25,
                         'reason_counts': {'OTHER_REASON': 25}})
    assert out['dominant_reason'] == 'other_reason'


def test_batch_evaluate_aggregates():
    rows = [
        {'sku': 'A', 'sold_30d': 100, 'returned_30d': 30,
         'reason_counts': {'QUALITY': 30}},
        {'sku': 'B', 'sold_30d': 100, 'returned_30d': 12,
         'reason_counts': {'SIZE': 12}},
        {'sku': 'C', 'sold_30d': 100, 'returned_30d': 1},
    ]
    out = batch_evaluate(rows)
    assert out['count'] == 3
    assert out['by_decision'].get('pause') == 1
    assert out['by_decision'].get('review') == 1
    assert out['by_decision'].get('keep') == 1
    assert len(out['pause_candidates']) == 1
    assert out['pause_candidates'][0]['sku'] == 'A'
