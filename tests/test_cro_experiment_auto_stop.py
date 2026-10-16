"""S78 — experiment auto-stop tests."""
from __future__ import annotations

import json

from src.services.cro_experiment_auto_stop import (
    auto_stop_experiments, evaluate_stop, stop_experiment,
)


def test_evaluate_no_trigger():
    out = evaluate_stop({'lift_pct': 0.05, 'returns_pct': 0.05,
                         'loss_usd': 10},
                        {'max_negative_lift_pct': -0.10})
    assert out['stop'] is False
    assert out['reasons'] == []


def test_evaluate_negative_lift_triggers():
    out = evaluate_stop({'lift_pct': -0.15},
                        {'max_negative_lift_pct': -0.10})
    assert out['stop']
    assert 'lift_pct' in out['reasons'][0]


def test_evaluate_high_returns_triggers():
    out = evaluate_stop({'returns_pct': 0.25},
                        {'max_returns_pct': 0.20})
    assert out['stop']


def test_evaluate_loss_triggers():
    out = evaluate_stop({'loss_usd': 300}, {'max_loss_usd': 200})
    assert out['stop']


def test_evaluate_multiple_reasons():
    out = evaluate_stop({'lift_pct': -0.20, 'loss_usd': 500},
                        {'max_negative_lift_pct': -0.10,
                         'max_loss_usd': 200})
    assert len(out['reasons']) == 2


def test_evaluate_missing_metric_skips_condition():
    out = evaluate_stop({}, {'max_negative_lift_pct': -0.10})
    assert out['stop'] is False


def test_stop_experiment_writes_log_and_pauses(tmp_path):
    log = tmp_path / 'p.jsonl'
    paused = []
    rec = stop_experiment('exp1', ['lift drop'],
                          pause_callable=lambda eid: paused.append(eid) or True,
                          blacklist_writer=lambda s, r: None,
                          affected_skus=['A', 'B'],
                          log_path=log)
    assert rec['paused'] is True
    assert rec['blacklisted_skus'] == ['A', 'B']
    line = json.loads(log.read_text(encoding='utf-8').strip())
    assert line['experiment_id'] == 'exp1'


def test_stop_experiment_pause_exception_does_not_propagate(tmp_path):
    log = tmp_path / 'p.jsonl'

    def bad(eid):
        raise RuntimeError('api down')

    rec = stop_experiment('exp1', ['x'],
                          pause_callable=bad,
                          log_path=log)
    assert rec['paused'] is False


def test_stop_experiment_blacklist_writer_exception_skips(tmp_path):
    log = tmp_path / 'p.jsonl'

    def bad(sku, reason):
        if sku == 'B':
            raise RuntimeError('db down')

    rec = stop_experiment('exp1', ['x'],
                          blacklist_writer=bad,
                          affected_skus=['A', 'B', 'C'],
                          log_path=log)
    assert 'A' in rec['blacklisted_skus']
    assert 'B' not in rec['blacklisted_skus']
    assert 'C' in rec['blacklisted_skus']


def test_auto_stop_experiments_aggregates(tmp_path):
    log = tmp_path / 'p.jsonl'
    exps = [
        {'experiment_id': 'e1',
         'metrics': {'lift_pct': -0.20},
         'stop_conditions': {'max_negative_lift_pct': -0.10},
         'skus': ['A']},
        {'experiment_id': 'e2',
         'metrics': {'lift_pct': 0.20},
         'stop_conditions': {'max_negative_lift_pct': -0.10},
         'skus': ['B']},
    ]
    out = auto_stop_experiments(exps, log_path=log)
    assert out['inspected'] == 2
    assert out['stopped_count'] == 1
    assert out['stopped'][0]['experiment_id'] == 'e1'
