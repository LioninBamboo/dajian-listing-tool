"""S131 — canary release tests."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.services.cro_canary_release import (
    STAGES, append_history, evaluate_health, init_canary,
    promote_next_stage, rollback,
)


def _good_metrics():
    return {'error_rate': 0.005, 'p95_latency_ms': 500, 'success_count': 100}


def test_init_invalid_stage():
    with pytest.raises(ValueError):
        init_canary('r1', stage='wrong')


def test_init_default_stage():
    s = init_canary('r1', now=datetime(2026, 1, 1))
    assert s['stage'] == 'canary_10'
    assert s['pct'] == 10
    assert len(s['history']) == 1


def test_evaluate_health_pass():
    h = evaluate_health(_good_metrics())
    assert h['healthy'] is True


def test_evaluate_health_error_rate_fails():
    h = evaluate_health({'error_rate': 0.10, 'p95_latency_ms': 100,
                          'success_count': 100})
    assert h['healthy'] is False
    assert any('error_rate' in i for i in h['issues'])


def test_promote_blocked_by_hold():
    now = datetime(2026, 1, 1, 12, 0)
    s = init_canary('r1', now=now)
    out = promote_next_stage(s, _good_metrics(),
                              now=now + timedelta(minutes=1))
    assert out['promoted'] is False
    assert 'hold_pending' in out['reason']


def test_promote_succeeds_after_hold():
    now = datetime(2026, 1, 1, 12, 0)
    s = init_canary('r1', now=now)
    out = promote_next_stage(s, _good_metrics(),
                              now=now + timedelta(hours=2))
    assert out['promoted'] is True
    assert out['state']['stage'] == 'canary_30'


def test_promote_blocked_by_unhealthy():
    now = datetime(2026, 1, 1, 12, 0)
    s = init_canary('r1', now=now)
    out = promote_next_stage(s,
                              {'error_rate': 0.5, 'p95_latency_ms': 100,
                               'success_count': 100},
                              now=now + timedelta(hours=2))
    assert out['promoted'] is False
    assert out['reason'] == 'unhealthy'


def test_promote_full_to_full_noop():
    now = datetime(2026, 1, 1, 12, 0)
    s = init_canary('r1', stage='full_100', now=now)
    out = promote_next_stage(s, _good_metrics(),
                              now=now + timedelta(hours=2))
    assert out['promoted'] is False
    assert out['reason'] == 'already_full'


def test_promote_walks_full_chain():
    now = datetime(2026, 1, 1, 12, 0)
    s = init_canary('r1', now=now)
    for _ in range(3):
        now = now + timedelta(hours=2)
        out = promote_next_stage(s, _good_metrics(), now=now)
        assert out['promoted']
        s = out['state']
    assert s['stage'] == 'full_100'
    # 4th promote on full → already_full
    out = promote_next_stage(s, _good_metrics(),
                              now=now + timedelta(hours=2))
    assert out['reason'] == 'already_full'


def test_rollback_blocks_further_promote():
    now = datetime(2026, 1, 1, 12, 0)
    s = init_canary('r1', now=now)
    s = rollback(s, 'high error rate', now=now + timedelta(minutes=10))
    assert s['rolled_back'] is True
    assert s['pct'] == 0
    out = promote_next_stage(s, _good_metrics(),
                              now=now + timedelta(hours=5))
    assert out['promoted'] is False
    assert out['reason'] == 'rolled_back'


def test_history_jsonl_append(tmp_path):
    p = str(tmp_path / 'canary.jsonl')
    s = init_canary('r1')
    assert append_history(p, s) is True
    assert open(p).read().strip() != ''
