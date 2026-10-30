"""S40 \u2014 chaos drill tests."""
from __future__ import annotations

from pathlib import Path

from src.services.cro_chaos_drill import (
    db_with_busy_timeout, run_drill, safe_invoke_with_retry,
    validate_queue_lines,
)


def test_safe_invoke_succeeds_first_try():
    rep = safe_invoke_with_retry(lambda: 42, retries=3)
    assert rep == {'ok': True, 'value': 42, 'attempts': 1}


def test_safe_invoke_retries_then_succeeds():
    n = {'i': 0}
    def flaky():
        n['i'] += 1
        if n['i'] < 2:
            raise RuntimeError('boom')
        return 'good'
    rep = safe_invoke_with_retry(flaky, retries=3)
    assert rep['ok']
    assert rep['attempts'] == 2


def test_safe_invoke_exhausts_retries():
    def always_fail():
        raise ValueError('always')
    rep = safe_invoke_with_retry(always_fail, retries=3)
    assert not rep['ok']
    assert 'ValueError' in rep['last_error']
    assert rep['attempts'] == 3


def test_validate_queue_detects_corrupt(tmp_path: Path):
    p = tmp_path / 'q.jsonl'
    p.write_text(
        '{"sku": "A"}\n'
        'NOT JSON HERE\n'
        '{"missing": "sku"}\n'
        '{"sku": "B"}\n',
        encoding='utf-8',
    )
    rep = validate_queue_lines(p)
    assert rep['total'] == 4
    assert rep['valid'] == 2
    assert sorted(rep['corrupt_lines']) == [2, 3]


def test_validate_queue_missing_file(tmp_path: Path):
    rep = validate_queue_lines(tmp_path / 'no.jsonl')
    assert rep == {'total': 0, 'valid': 0, 'corrupt_lines': []}


def test_db_with_busy_timeout_works(tmp_path: Path):
    db = tmp_path / 'e.db'
    with db_with_busy_timeout(db, timeout_ms=2000) as c:
        c.execute("CREATE TABLE t(id INTEGER)")
        c.execute("INSERT INTO t VALUES(1)")
    with db_with_busy_timeout(db) as c:
        rows = c.execute("SELECT id FROM t").fetchall()
    assert rows == [(1,)]


def test_run_drill_passes_when_all_healthy(tmp_path: Path):
    q = tmp_path / 'q.jsonl'
    q.write_text('{"sku": "A"}\n', encoding='utf-8')
    rep = run_drill(jsonl_path=q, flaky_fn=lambda: 1, db_path=tmp_path / 'd.db')
    assert rep['passed']


def test_run_drill_fails_when_queue_corrupt(tmp_path: Path):
    q = tmp_path / 'q.jsonl'
    q.write_text('NOT JSON\n', encoding='utf-8')
    rep = run_drill(jsonl_path=q)
    assert not rep['passed']
