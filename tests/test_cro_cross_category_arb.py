"""S118 — cross category arbitrage tests."""
from __future__ import annotations

from src.services.cro_cross_category_arb import (
    find_arbitrage_opportunities, summarise,
)


def test_no_opportunities_when_baselines_empty():
    rows = [{'sku': 'A', 'category_id': 1, 'current_price': 10}]
    assert find_arbitrage_opportunities(rows, {}) == []


def test_finds_opportunity_with_high_lift():
    rows = [{'sku': 'A', 'category_id': 1, 'current_price': 10,
              'sold_30d': 5}]
    baselines = {2: {'median_price': 15, 'sample': 50}}
    out = find_arbitrage_opportunities(rows, baselines)
    assert len(out) == 1
    assert out[0]['target_category'] == 2
    assert out[0]['lift'] == 0.50
    assert out[0]['recommended_price'] == round(15 * 0.95, 2)


def test_skips_when_lift_below_threshold():
    rows = [{'sku': 'A', 'category_id': 1, 'current_price': 10}]
    baselines = {2: {'median_price': 11, 'sample': 50}}  # 10% lift
    out = find_arbitrage_opportunities(rows, baselines, min_lift=0.20)
    assert out == []


def test_skips_when_sample_too_small():
    rows = [{'sku': 'A', 'category_id': 1, 'current_price': 10}]
    baselines = {2: {'median_price': 20, 'sample': 5}}
    out = find_arbitrage_opportunities(rows, baselines, min_sample=10)
    assert out == []


def test_picks_best_lift_when_multiple_targets():
    rows = [{'sku': 'A', 'category_id': 1, 'current_price': 10}]
    baselines = {
        2: {'median_price': 15, 'sample': 50},  # 50% lift
        3: {'median_price': 25, 'sample': 50},  # 150% lift
    }
    out = find_arbitrage_opportunities(rows, baselines)
    assert out[0]['target_category'] == 3


def test_skips_zero_price():
    rows = [{'sku': 'A', 'category_id': 1, 'current_price': 0}]
    baselines = {2: {'median_price': 10, 'sample': 50}}
    assert find_arbitrage_opportunities(rows, baselines) == []


def test_sorted_by_lift_desc():
    rows = [
        {'sku': 'A', 'category_id': 1, 'current_price': 10},
        {'sku': 'B', 'category_id': 1, 'current_price': 10},
    ]
    baselines = {
        2: {'median_price': 12.5, 'sample': 50},  # 25% for A
        3: {'median_price': 30, 'sample': 50},    # 200% for B
    }
    rows[0]['_target'] = 2  # noise
    out = find_arbitrage_opportunities(rows, baselines)
    assert out[0]['lift'] >= out[1]['lift']


def test_summarise_empty():
    assert summarise([])['count'] == 0


def test_summarise_computes_potential():
    opps = [
        {'current_price': 10, 'recommended_price': 14, 'sold_30d': 10,
         'lift': 0.4},
    ]
    s = summarise(opps)
    assert s['count'] == 1
    assert s['total_potential_lift_usd'] == 40.0
