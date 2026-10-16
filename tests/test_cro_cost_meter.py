"""S70 — cost meter tests."""
from __future__ import annotations

import json

from src.services.cro_cost_meter import (
    cost_for_compute, cost_for_llm, downgrade_recommendation,
    record_decision_cost, summarise_costs,
)


def test_cost_for_llm_basic():
    # 1000 input + 1000 output = 0.0005 + 0.0015 = 0.002
    c = cost_for_llm(1000, 1000)
    assert abs(c - 0.002) < 1e-9


def test_cost_for_llm_custom_prices():
    p = {'llm_input_per_1k_tokens': 0.001,
         'llm_output_per_1k_tokens': 0.002}
    assert abs(cost_for_llm(2000, 1000, p) - (0.002 + 0.002)) < 1e-9


def test_cost_for_compute_no_negative():
    assert cost_for_compute(-5) == 0.0


def test_record_decision_cost_writes_jsonl(tmp_path):
    log = tmp_path / 'c.jsonl'
    rec = record_decision_cost(
        'd1', 'promote',
        category='Kitchen', llm_input_tokens=500, llm_output_tokens=200,
        compute_seconds=2.0, log_path=log,
    )
    assert rec['cost_usd'] > 0
    assert log.exists()
    line = json.loads(log.read_text(encoding='utf-8').splitlines()[0])
    assert line['action'] == 'promote'
    assert line['decision_id'] == 'd1'


def test_summarise_costs_aggregates_by_action(tmp_path):
    log = tmp_path / 'c.jsonl'
    record_decision_cost('d1', 'promote', llm_input_tokens=1000,
                         llm_output_tokens=1000, log_path=log)
    record_decision_cost('d2', 'promote', llm_input_tokens=2000,
                         llm_output_tokens=2000, log_path=log)
    record_decision_cost('d3', 'price_drop', llm_input_tokens=500,
                         llm_output_tokens=500, log_path=log)
    s = summarise_costs(log_path=log)
    assert s['count'] == 3
    assert s['by_action']['promote']['count'] == 2
    assert s['by_action']['price_drop']['count'] == 1
    assert s['avg_cost_per_decision'] > 0


def test_summarise_costs_empty_log(tmp_path):
    s = summarise_costs(log_path=tmp_path / 'missing.jsonl')
    assert s['count'] == 0
    assert s['total_cost'] == 0
    assert s['avg_cost_per_decision'] == 0


def test_summarise_costs_skips_corrupt_lines(tmp_path):
    log = tmp_path / 'c.jsonl'
    log.write_text('NOT_JSON\n{"cost_usd": 0.01, "action": "promote"}\n',
                   encoding='utf-8')
    s = summarise_costs(log_path=log)
    assert s['count'] == 1


def test_downgrade_recommendation_under_budget():
    s = {'avg_cost_per_decision': 0.001}
    out = downgrade_recommendation(s, threshold_avg_usd=0.005)
    assert out['suggest_downgrade_to_rules'] is False


def test_downgrade_recommendation_over_budget():
    s = {'avg_cost_per_decision': 0.020}
    out = downgrade_recommendation(s, threshold_avg_usd=0.005)
    assert out['suggest_downgrade_to_rules'] is True
    assert '0.0050' in out['reason']
