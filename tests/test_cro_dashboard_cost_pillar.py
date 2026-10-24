"""S80 — cost pillar tests."""
from __future__ import annotations

from src.services.cro_dashboard_cost_pillar import (
    cost_pillar, merge_into_traffic_light, status_for_cost_ratio,
)


def test_status_thresholds():
    assert status_for_cost_ratio(0.0) == 'green'
    assert status_for_cost_ratio(0.50) == 'green'
    assert status_for_cost_ratio(0.80) == 'yellow'
    assert status_for_cost_ratio(0.99) == 'yellow'
    assert status_for_cost_ratio(1.0) == 'red'
    assert status_for_cost_ratio(1.50) == 'red'


def test_cost_pillar_explicit_values_under_budget():
    p = cost_pillar(today_cost_usd=1.0, daily_budget_usd=10.0)
    assert p['status'] == 'green'
    assert p['used_ratio'] == 0.1
    assert '$1.0000' in p['message']


def test_cost_pillar_at_budget_red():
    p = cost_pillar(today_cost_usd=10.0, daily_budget_usd=10.0)
    assert p['status'] == 'red'
    assert p['used_ratio'] == 1.0


def test_cost_pillar_yellow_zone():
    p = cost_pillar(today_cost_usd=8.5, daily_budget_usd=10.0)
    assert p['status'] == 'yellow'


def test_cost_pillar_zero_budget():
    p = cost_pillar(today_cost_usd=1.0, daily_budget_usd=0)
    assert p['used_ratio'] == 0.0
    assert p['status'] == 'green'


def test_cost_pillar_reads_log_when_today_cost_none(tmp_path):
    log = tmp_path / 'c.jsonl'
    log.write_text('{"cost_usd": 0.5, "action": "promote"}\n'
                   '{"cost_usd": 0.7, "action": "promote"}\n',
                   encoding='utf-8')
    p = cost_pillar(daily_budget_usd=2.0, cost_log_path=log)
    assert abs(p['today_cost_usd'] - 1.2) < 1e-6
    assert p['avg_cost_per_decision'] > 0


def test_merge_into_traffic_light_recomputes_overall():
    tl = {'status_overall': 'green',
          'pillars': [{'pillar': 'returns', 'status': 'green'}]}
    cost = {'pillar': 'cost', 'status': 'red'}
    out = merge_into_traffic_light(tl, cost)
    assert out['status_overall'] == 'red'
    assert len(out['pillars']) == 2


def test_merge_keeps_yellow_if_other_pillar_yellow():
    tl = {'status_overall': 'yellow',
          'pillars': [{'pillar': 'returns', 'status': 'yellow'}]}
    cost = {'pillar': 'cost', 'status': 'green'}
    out = merge_into_traffic_light(tl, cost)
    assert out['status_overall'] == 'yellow'
