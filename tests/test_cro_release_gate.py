"""S130 — release gate tests."""
from __future__ import annotations

from src.services.cro_release_gate import evaluate_release, run_release_check


def test_all_clear_green():
    out = evaluate_release(pytest_result={'failed': 0},
                            invariant_result={'failed_count': 0},
                            sentinel_critical_count=0)
    assert out['green'] is True
    assert out['blocked_by'] == []
    assert 'READY' in out['summary']


def test_pytest_failure_blocks():
    out = evaluate_release(pytest_result={'failed': 3},
                            invariant_result={'failed_count': 0},
                            sentinel_critical_count=0)
    assert out['green'] is False
    assert any('pytest_failed' in b for b in out['blocked_by'])


def test_invariants_block():
    out = evaluate_release(pytest_result={'failed': 0},
                            invariant_result={'failed_count': 2},
                            sentinel_critical_count=0)
    assert any('invariants_violated' in b for b in out['blocked_by'])


def test_sentinel_critical_blocks():
    out = evaluate_release(pytest_result={'failed': 0},
                            invariant_result={'failed_count': 0},
                            sentinel_critical_count=1)
    assert any('sentinel_critical' in b for b in out['blocked_by'])


def test_relaxed_thresholds_allow():
    out = evaluate_release(pytest_result={'failed': 1},
                            invariant_result={'failed_count': 1},
                            sentinel_critical_count=1,
                            max_failed=1, max_violations=1, max_critical=1)
    assert out['green'] is True


def test_summary_lists_all_blockers():
    out = evaluate_release(pytest_result={'failed': 1},
                            invariant_result={'failed_count': 1},
                            sentinel_critical_count=1)
    assert len(out['blocked_by']) == 3


def test_metrics_reported():
    out = evaluate_release(pytest_result={'failed': 2},
                            invariant_result={'failed_count': 1},
                            sentinel_critical_count=3)
    assert out['metrics'] == {'pytest_failed': 2,
                                'invariant_violations': 1,
                                'sentinel_critical': 3}


def test_run_release_check_all_callable():
    out = run_release_check(
        pytest_callable=lambda: {'failed': 0},
        invariant_callable=lambda: {'failed_count': 0},
        sentinel_callable=lambda: 0,
    )
    assert out['green'] is True
    assert out['raw']['sentinel_critical'] == 0


def test_run_release_check_callable_exception_blocks():
    def boom():
        raise RuntimeError('x')
    out = run_release_check(
        pytest_callable=boom,
        invariant_callable=lambda: {'failed_count': 0},
        sentinel_callable=lambda: 0,
    )
    assert out['green'] is False


def test_missing_keys_default_to_zero():
    out = evaluate_release(pytest_result={},
                            invariant_result={},
                            sentinel_critical_count=0)
    assert out['green'] is True
