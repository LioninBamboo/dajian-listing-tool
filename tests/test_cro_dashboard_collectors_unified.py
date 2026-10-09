"""S126 — unified dashboard collectors tests."""
from __future__ import annotations

from src.services.cro_dashboard_collectors_unified import (
    PILLARS, collect_unified,
)


def test_no_fetchers_all_missing():
    out = collect_unified()
    assert out['available_count'] == 0
    assert set(out['missing']) == set(PILLARS)
    assert out['health_ratio'] == 0.0


def test_all_fetchers_present():
    out = collect_unified(
        traffic_light_fetcher=lambda: {'green': 5},
        baseline_fetcher=lambda: {'mu': 10.0},
        lqi_fetcher=lambda: {'avg': 80},
        arb_fetcher=lambda: [{'sku': 'A'}],
        returns_fetcher=lambda: {'rate': 0.05},
    )
    assert out['available_count'] == 5
    assert out['health_ratio'] == 1.0
    assert out['missing'] == []


def test_failing_fetcher_yields_none():
    def boom():
        raise RuntimeError('x')
    out = collect_unified(traffic_light_fetcher=boom)
    assert out['pillars']['traffic_light'] is None
    assert 'traffic_light' in out['missing']


def test_partial_health():
    out = collect_unified(
        traffic_light_fetcher=lambda: 'ok',
        baseline_fetcher=lambda: 'ok',
        lqi_fetcher=lambda: 'ok',
    )
    assert out['available_count'] == 3
    assert out['health_ratio'] == 0.6


def test_pillars_returned_in_dict_form():
    out = collect_unified(arb_fetcher=lambda: [1, 2])
    assert isinstance(out['pillars'], dict)
    assert set(out['pillars'].keys()) == set(PILLARS)


def test_zero_value_not_treated_as_missing():
    out = collect_unified(returns_fetcher=lambda: 0)
    # 0 is not None → should be available
    assert 'returns' in out['available']


def test_empty_dict_not_treated_as_missing():
    out = collect_unified(arb_fetcher=lambda: {})
    assert 'arb' in out['available']


def test_available_list_alphabetical_or_stable():
    out = collect_unified(traffic_light_fetcher=lambda: 'x',
                            returns_fetcher=lambda: 'y')
    assert set(out['available']) == {'traffic_light', 'returns'}
