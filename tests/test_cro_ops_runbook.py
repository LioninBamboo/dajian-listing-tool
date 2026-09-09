"""S132 — ops runbook tests."""
from __future__ import annotations

import pytest

from src.services.cro_ops_runbook import RunbookRegistry, execute_runbook


def test_register_validation():
    r = RunbookRegistry()
    with pytest.raises(ValueError):
        r.register('', [{'name': 's', 'fn': lambda: None}])
    with pytest.raises(ValueError):
        r.register('rb', [])
    with pytest.raises(ValueError):
        r.register('rb', [{'fn': lambda: None}])
    with pytest.raises(TypeError):
        r.register('rb', [{'name': 's', 'fn': 'not callable'}])


def test_register_and_get():
    r = RunbookRegistry()
    r.register('a', [{'name': 's1', 'fn': lambda: 1}])
    r.register('b', [{'name': 's1', 'fn': lambda: 1}])
    assert r.names() == ['a', 'b']
    with pytest.raises(KeyError):
        r.get('nope')


def test_execute_all_ok():
    out = execute_runbook([
        {'name': 's1', 'fn': lambda: 'x'},
        {'name': 's2', 'fn': lambda: 'y'},
    ])
    assert out['ok'] == 2
    assert out['failed'] == 0
    assert out['aborted'] is False


def test_execute_critical_failure_aborts():
    calls = []
    out = execute_runbook([
        {'name': 's1', 'fn': lambda: calls.append('s1')},
        {'name': 's2', 'fn': lambda: 1 / 0, 'critical': True},
        {'name': 's3', 'fn': lambda: calls.append('s3')},
    ])
    assert out['aborted'] is True
    assert out['abort_reason'] == 's2'
    assert calls == ['s1']
    assert out['results'][2]['status'] == 'skipped_after_abort'


def test_non_critical_failure_continues():
    calls = []
    out = execute_runbook([
        {'name': 's1', 'fn': lambda: 1 / 0, 'critical': False},
        {'name': 's2', 'fn': lambda: calls.append('s2')},
    ])
    assert out['aborted'] is False
    assert out['failed'] == 1
    assert calls == ['s2']


def test_dry_run_skips_execution():
    calls = []
    out = execute_runbook([
        {'name': 's1', 'fn': lambda: calls.append('s1')},
    ], dry_run=True)
    assert calls == []
    assert out['results'][0]['status'] == 'dry_run'


def test_step_args_kwargs_passed():
    captured = {}
    def fn(a, b=None):
        captured['a'] = a
        captured['b'] = b
        return a + (b or 0)
    out = execute_runbook([
        {'name': 's1', 'fn': fn, 'args': (10,), 'kwargs': {'b': 5}},
    ])
    assert captured == {'a': 10, 'b': 5}
    assert out['results'][0]['return'] == 15


def test_log_path_writes_jsonl(tmp_path):
    p = str(tmp_path / 'rb.jsonl')
    execute_runbook([{'name': 's1', 'fn': lambda: 1}], log_path=p)
    assert open(p).read().strip() != ''


def test_non_jsonable_return_stringified():
    class X:
        def __repr__(self):
            return '<X>'
    out = execute_runbook([{'name': 's1', 'fn': lambda: X()}])
    assert str(out['results'][0]['return']) == '<X>'


def test_default_critical_true():
    out = execute_runbook([
        {'name': 's1', 'fn': lambda: 1 / 0},   # default critical=True
        {'name': 's2', 'fn': lambda: 1},
    ])
    assert out['aborted'] is True
