"""S122 — actions executor router tests."""
from __future__ import annotations

import pytest

from src.services.cro_actions_executor_router import ActionRouter
from src.services.cro_event_bus import EventBus


def test_register_non_callable_raises():
    r = ActionRouter()
    with pytest.raises(TypeError):
        r.register('x', 'not callable')


def test_default_noop_when_no_executor():
    r = ActionRouter()
    out = r.execute_one({'sku': 'A', 'action_type': 'unknown'})
    assert out['status'] == 'skipped'
    assert out['reason'] == 'no_executor_registered'


def test_executor_ok_status():
    r = ActionRouter()
    r.register('price_charm', lambda row: {'status': 'ok', 'new_price': 9.99})
    out = r.execute_one({'sku': 'A', 'action_type': 'price_charm'})
    assert out['status'] == 'ok'
    assert out['new_price'] == 9.99


def test_executor_exception_marks_failed():
    r = ActionRouter()
    r.register('boom', lambda row: 1 / 0)
    out = r.execute_one({'sku': 'A', 'action_type': 'boom'})
    assert out['status'] == 'failed'
    assert 'ZeroDivisionError' in out['error']


def test_event_bus_receives_executed():
    bus = EventBus()
    captured = []
    bus.subscribe('action.executed', captured.append)
    bus.subscribe('action.failed', captured.append)
    r = ActionRouter(event_bus=bus)
    r.register('ok', lambda _: {'status': 'ok'})
    r.register('bad', lambda _: 1 / 0)
    r.execute_one({'sku': 'A', 'action_type': 'ok'})
    r.execute_one({'sku': 'B', 'action_type': 'bad'})
    statuses = {c['status'] for c in captured}
    assert statuses == {'ok', 'failed'}


def test_event_bus_failure_does_not_break_router():
    class BadBus:
        def publish(self, *a, **kw):
            raise RuntimeError('bus down')
    r = ActionRouter(event_bus=BadBus())
    r.register('ok', lambda _: {'status': 'ok'})
    out = r.execute_one({'sku': 'A', 'action_type': 'ok'})
    assert out['status'] == 'ok'   # 不被 bus 异常影响


def test_execute_batch_aggregates():
    r = ActionRouter()
    r.register('a', lambda _: {'status': 'ok'})
    r.register('b', lambda _: 1 / 0)
    rows = [
        {'sku': 'X', 'action_type': 'a'},
        {'sku': 'Y', 'action_type': 'b'},
        {'sku': 'Z', 'action_type': 'unknown'},
    ]
    out = r.execute_batch(rows)
    assert out['count'] == 3
    assert out['ok'] == 1
    assert out['failed'] == 1
    assert out['skipped'] == 1


def test_execute_batch_dry_run_skips_executors():
    calls = []
    r = ActionRouter()
    r.register('a', lambda row: calls.append(row) or {'status': 'ok'})
    out = r.execute_batch([{'sku': 'X', 'action_type': 'a'}], dry_run=True)
    assert calls == []
    assert out['results'][0]['status'] == 'dry_run'


def test_has_executor():
    r = ActionRouter()
    r.register('x', lambda _: {'status': 'ok'})
    assert r.has_executor('x') is True
    assert r.has_executor('y') is False


def test_executor_returning_none_treated_as_ok():
    r = ActionRouter()
    r.register('x', lambda _: None)
    out = r.execute_one({'sku': 'A', 'action_type': 'x'})
    assert out['status'] == 'ok'
