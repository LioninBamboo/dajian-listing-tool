"""S123 — run full loop tests."""
from __future__ import annotations

import pytest

from src.services.cro_run_full_loop import (
    render_run_report, run_full_loop,
)


def _pipeline_ok():
    return {'rows': [{'sku': 'A', 'action_type': 'price_charm'}],
            'count': 1, 'by_action': {'price_charm': 1},
            'skipped_blacklisted': 0, 'skipped_duplicate': 0}


def _executor_ok(rows, dry_run):
    return {'results': rows, 'count': len(rows),
            'ok': 0 if dry_run else len(rows),
            'failed': 0, 'skipped': 0, 'dry_run': dry_run}


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        run_full_loop(build_pipeline_fn=_pipeline_ok,
                       router_execute_batch_fn=_executor_ok,
                       mode='wrong')


def test_dry_run_does_not_apply():
    out = run_full_loop(build_pipeline_fn=_pipeline_ok,
                         router_execute_batch_fn=_executor_ok,
                         mode='dry_run')
    assert out['mode'] == 'dry_run'
    assert out['executor']['ok'] == 0
    assert out['executor']['dry_run'] is True


def test_apply_mode_executes():
    out = run_full_loop(build_pipeline_fn=_pipeline_ok,
                         router_execute_batch_fn=_executor_ok,
                         mode='apply')
    assert out['executor']['ok'] == 1


def test_pipeline_exception_does_not_crash():
    def bad_pipeline():
        raise RuntimeError('boom')
    out = run_full_loop(build_pipeline_fn=bad_pipeline,
                         router_execute_batch_fn=_executor_ok)
    assert out['pipeline']['count'] == 0
    assert 'error' in out['pipeline']


def test_executor_exception_does_not_crash():
    def bad_exec(rows, dry):
        raise RuntimeError('boom')
    out = run_full_loop(build_pipeline_fn=_pipeline_ok,
                         router_execute_batch_fn=bad_exec)
    assert out['executor']['count'] == 0
    assert out['executor']['ok'] == 0
    assert 'error' in out['executor']


def test_email_sender_called_with_subject_and_body():
    captured = {}
    def sender(subj, body):
        captured['subj'] = subj
        captured['body'] = body
        return True
    out = run_full_loop(build_pipeline_fn=_pipeline_ok,
                         router_execute_batch_fn=_executor_ok,
                         email_sender=sender)
    assert out['email_sent'] is True
    assert 'CRO Full Loop' in captured['subj']
    assert 'Total actions enqueued' in captured['body']


def test_email_sender_failure_recorded():
    def sender(subj, body):
        raise RuntimeError('smtp down')
    out = run_full_loop(build_pipeline_fn=_pipeline_ok,
                         router_execute_batch_fn=_executor_ok,
                         email_sender=sender)
    assert out['email_sent'] is False


def test_render_report_contains_key_metrics():
    p = {'count': 5, 'by_action': {'a': 3, 'b': 2},
         'skipped_blacklisted': 1, 'skipped_duplicate': 0}
    e = {'ok': 4, 'failed': 1, 'skipped': 0, 'dry_run': False}
    report = render_run_report(p, e)
    assert 'Total actions enqueued: 5' in report
    assert 'ok: 4' in report
    assert 'failed: 1' in report
