"""S104 — geo segment analyzer tests."""
from __future__ import annotations

from src.services.cro_geo_segment_analyzer import (
    aggregate_by_state, aggregate_by_zip3, analyze_geo, concentration_label,
    hhi,
)


SAMPLE = [
    {'state': 'CA', 'zip': '90001', 'qty': 1, 'revenue': 100},
    {'state': 'CA', 'zip': '90002', 'qty': 1, 'revenue': 200},
    {'state': 'TX', 'zip': '75001', 'qty': 1, 'revenue': 50},
    {'state': 'NY', 'zip': '10001', 'qty': 1, 'revenue': 30},
    {'state': '', 'zip': '', 'qty': 1, 'revenue': 10},
]


def test_aggregate_by_state_sums_and_sorts():
    out = aggregate_by_state(SAMPLE)
    assert out[0]['state'] == 'CA'
    assert out[0]['revenue'] == 300.0
    assert out[0]['orders'] == 2


def test_aggregate_by_state_empty_state_bucketed_to_unknown():
    out = aggregate_by_state(SAMPLE)
    assert any(s['state'] == 'UNKNOWN' for s in out)


def test_aggregate_by_zip3_groups_first_three():
    out = aggregate_by_zip3(SAMPLE)
    z3s = {b['zip3'] for b in out}
    assert '900' in z3s  # 90001 + 90002
    assert '750' in z3s
    z900 = next(b for b in out if b['zip3'] == '900')
    assert z900['orders'] == 2


def test_hhi_zero_when_no_revenue():
    assert hhi([]) == 0.0


def test_hhi_high_when_one_state_dominates():
    by_state = [{'revenue': 1000}, {'revenue': 1}]
    assert hhi(by_state) >= 9000


def test_concentration_labels():
    assert concentration_label(3000) == 'highly_concentrated'
    assert concentration_label(2000) == 'moderately_concentrated'
    assert concentration_label(800) == 'diversified'


def test_analyze_geo_full():
    out = analyze_geo(SAMPLE, top_n=2)
    assert out['state_count'] == 4  # CA TX NY UNKNOWN
    assert out['total_orders'] == 5
    assert out['total_revenue'] == 390.0
    assert len(out['top_states']) == 2
    assert out['top_states'][0]['state'] == 'CA'
    assert out['concentration'] in ('highly_concentrated',
                                     'moderately_concentrated',
                                     'diversified')


def test_analyze_geo_empty():
    out = analyze_geo([])
    assert out['state_count'] == 0
    assert out['total_revenue'] == 0
    assert out['hhi'] == 0.0
