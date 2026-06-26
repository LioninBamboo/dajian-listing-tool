"""S88 — retry queue tests."""
from __future__ import annotations

import random

from src.services.cro_retry_queue import (
    compute_backoff, enqueue_failed, load_pending, with_retry,
)


def test_compute_backoff_grows_exponentially():
    a0 = compute_backoff(0, base=1, factor=2, jitter_ratio=0)
    a1 = compute_backoff(1, base=1, factor=2, jitter_ratio=0)
    a2 = compute_backoff(2, base=1, factor=2, jitter_ratio=0)
    assert a0 == 1
    assert a1 == 2
    assert a2 == 4


def test_compute_backoff_capped_at_max():
    delay = compute_backoff(20, base=1, factor=2, max_delay=10,
                            jitter_ratio=0)
    assert delay == 10


def test_compute_backoff_jitter_within_range():
    rng = random.Random(42)
    delay = compute_backoff(2, base=1, factor=2, jitter_ratio=0.1, rng=rng)
    assert 4 * 0.9 <= delay <= 4 * 1.1


def test_with_retry_succeeds_first_try():
    out = with_retry(lambda: 'hi', sleep=lambda s: None)
    assert out == {'ok': True, 'result': 'hi', 'attempts': 1,
                   'last_error': None}


def test_with_retry_succeeds_after_failures():
    counter = {'n': 0}

    def fn():
        counter['n'] += 1
        if counter['n'] < 3:
            raise RuntimeError('not yet')
        return 'ok'

    out = with_retry(fn, max_attempts=5, sleep=lambda s: None)
    assert out['ok'] is True
    assert out['attempts'] == 3


def test_with_retry_exhausts_max_attempts():
    out = with_retry(lambda: (_ for _ in ()).throw(RuntimeError('x')),
                     max_attempts=3, sleep=lambda s: None)
    assert out['ok'] is False
    assert out['attempts'] == 3
    assert 'RuntimeError' in out['last_error']


def test_with_retry_uses_sleep_between_attempts():
    sleeps = []

    def fn():
        raise RuntimeError('x')

    with_retry(fn, max_attempts=3, base=1, factor=2,
               jitter_ratio=0, sleep=sleeps.append)
    # 2 sleeps between 3 attempts; backoff(attempt=1)=2, backoff(attempt=2)=4
    assert sleeps == [2, 4]


def test_with_retry_does_not_swallow_unlisted_exceptions():
    def fn():
        raise KeyError('not retried')
    try:
        with_retry(fn, retry_on=(ValueError,), sleep=lambda s: None)
    except KeyError:
        pass
    else:
        assert False, 'KeyError must propagate'


def test_enqueue_and_load_pending(tmp_path):
    log = tmp_path / 'q.jsonl'
    enqueue_failed('t1', {'x': 1}, error='boom', log_path=log)
    enqueue_failed('t2', {'y': 2}, error='boom2', log_path=log)
    pending = load_pending(log_path=log)
    assert len(pending) == 2
    assert {p['task_id'] for p in pending} == {'t1', 't2'}


def test_load_pending_skips_done():
    pass  # status filtering happens by 'status' field; tested via direct write


def test_load_pending_corrupt_lines_skipped(tmp_path):
    log = tmp_path / 'q.jsonl'
    log.write_text(
        'not json\n'
        '{"task_id":"t1","status":"pending"}\n'
        '{"task_id":"t2","status":"done"}\n',
        encoding='utf-8')
    pending = load_pending(log_path=log)
    assert len(pending) == 1
    assert pending[0]['task_id'] == 't1'
