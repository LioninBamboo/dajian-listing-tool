"""S95 — competitor baseline tests."""
from __future__ import annotations

from src.services.cro_competitor_baseline import (
    baseline_stats, detect_underpriced, fetch_competitor_baseline,
    position_for_price,
)


def test_stats_empty():
    s = baseline_stats([])
    assert s['count'] == 0
    assert s['median'] == 0.0


def test_stats_basic():
    s = baseline_stats([10, 20, 30, 40, 50])
    assert s['count'] == 5
    assert s['median'] == 30.0
    assert s['min'] == 10.0
    assert s['max'] == 50.0


def test_stats_skips_zero_and_none():
    s = baseline_stats([10, 0, None, 30])
    assert s['count'] == 2
    assert s['median'] == 20.0


def test_position_cheap():
    stats = baseline_stats([10, 20, 30, 40, 50])
    out = position_for_price(8, stats)
    assert out['position'] == 'cheap'


def test_position_above_median():
    stats = baseline_stats([10, 20, 30, 40, 50])
    out = position_for_price(35, stats)
    assert out['position'] == 'above_median'


def test_position_expensive():
    stats = baseline_stats([10, 20, 30, 40, 50])
    out = position_for_price(60, stats)
    assert out['position'] == 'expensive'


def test_position_unknown_when_no_data():
    out = position_for_price(20, baseline_stats([]))
    assert out['position'] == 'unknown'


def test_fetch_competitor_baseline_normal():
    def fetcher(cat):
        return [{'price': 10}, {'price': 20}, {'price': 30,
                                                 'discount_pct': 0.10}]
    out = fetch_competitor_baseline('Kitchen', fetcher)
    assert out['stats']['median'] == 20.0
    assert out['sample_size'] == 3


def test_fetch_competitor_baseline_fetcher_exception():
    def bad(cat):
        raise RuntimeError('api')
    out = fetch_competitor_baseline('Kitchen', bad)
    assert out['error'] == 'fetcher_failed'


def test_detect_underpriced_flags_skus_below_p25():
    baselines = {'Kitchen': {'stats': baseline_stats([10, 20, 30, 40, 50])}}
    listings = [
        {'sku': 'A', 'category': 'Kitchen', 'price': 8},   # below p25=20
        {'sku': 'B', 'category': 'Kitchen', 'price': 25},  # above
        {'sku': 'C', 'category': 'NoBaseline', 'price': 1},
    ]
    out = detect_underpriced(listings, baselines)
    assert {x['sku'] for x in out} == {'A'}
