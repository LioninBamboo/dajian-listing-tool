"""S109 — event bus tests."""
from __future__ import annotations

import pytest

from src.services.cro_event_bus import EventBus


def test_subscribe_and_publish_calls_handler():
    bus = EventBus()
    captured = []
    bus.subscribe('order.created', captured.append)
    out = bus.publish('order.created', {'sku': 'X'})
    assert captured == [{'sku': 'X'}]
    assert out['ok'] == 1
    assert out['failed'] == 0


def test_publish_no_subscribers_zero_fired():
    bus = EventBus()
    out = bus.publish('nobody.listening', {})
    assert out['fired'] == 0
    assert out['ok'] == 0


def test_handler_exception_does_not_block_others():
    bus = EventBus()
    captured = []

    def bad(_):
        raise RuntimeError('boom')

    bus.subscribe('e', bad)
    bus.subscribe('e', captured.append)
    bus.subscribe('e', bad)
    out = bus.publish('e', 'payload')
    assert captured == ['payload']
    assert out['ok'] == 1
    assert out['failed'] == 2


def test_errors_are_recorded():
    bus = EventBus()
    bus.subscribe('e', lambda _: 1 / 0)
    bus.publish('e', None)
    assert len(bus.errors) == 1
    assert 'ZeroDivisionError' in bus.errors[0]['error']


def test_unsubscribe_removes_handler():
    bus = EventBus()
    h = lambda _: None
    bus.subscribe('e', h)
    assert bus.handler_count('e') == 1
    assert bus.unsubscribe('e', h) is True
    assert bus.handler_count('e') == 0


def test_unsubscribe_unknown_returns_false():
    bus = EventBus()
    assert bus.unsubscribe('missing', lambda _: None) is False


def test_subscribe_non_callable_raises():
    bus = EventBus()
    with pytest.raises(TypeError):
        bus.subscribe('e', 'not a function')


def test_event_names_lists_active_events():
    bus = EventBus()
    bus.subscribe('a', lambda _: None)
    bus.subscribe('b', lambda _: None)
    names = bus.event_names()
    assert 'a' in names and 'b' in names


def test_clear_resets_state():
    bus = EventBus()
    bus.subscribe('e', lambda _: 1 / 0)
    bus.publish('e', None)
    bus.clear()
    assert bus.event_names() == []
    assert bus.errors == []


def test_multiple_events_independent():
    bus = EventBus()
    a, b = [], []
    bus.subscribe('e1', a.append)
    bus.subscribe('e2', b.append)
    bus.publish('e1', 1)
    bus.publish('e2', 2)
    assert a == [1] and b == [2]
