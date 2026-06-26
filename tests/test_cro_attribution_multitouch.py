"""S92 — multitouch attribution tests."""
from __future__ import annotations

import pytest

from src.services.cro_attribution_multitouch import (
    attribute, compare_models,
)


def _conv(value, touches):
    return {'conversion_id': 'c', 'value': value,
            'touchpoints': [{'channel': c, 'ts': t}
                            for c, t in touches]}


def test_last_model_assigns_full_credit_to_last():
    convs = [_conv(100, [('email', '2026-01-01T00:00:00'),
                          ('ads', '2026-01-08T00:00:00')])]
    out = attribute(convs, model='last')
    assert out['by_channel']['ads'] == 100.0
    assert out['by_channel'].get('email', 0) == 0


def test_first_model_assigns_full_credit_to_first():
    convs = [_conv(100, [('email', '2026-01-01T00:00:00'),
                          ('ads', '2026-01-08T00:00:00')])]
    out = attribute(convs, model='first')
    assert out['by_channel']['email'] == 100.0
    assert out['by_channel'].get('ads', 0) == 0


def test_linear_model_splits_evenly():
    convs = [_conv(90, [('a', '2026-01-01T00:00:00'),
                         ('b', '2026-01-02T00:00:00'),
                         ('c', '2026-01-03T00:00:00')])]
    out = attribute(convs, model='linear')
    assert out['by_channel'] == {'a': 30.0, 'b': 30.0, 'c': 30.0}


def test_time_decay_recent_gets_more():
    convs = [_conv(100, [('old', '2026-01-01T00:00:00'),
                          ('new', '2026-01-08T00:00:00')])]
    out = attribute(convs, model='time_decay', half_life_days=7.0)
    # half-life=7d: old weight=0.5, new=1.0 → 33.33/66.66
    assert out['by_channel']['new'] > out['by_channel']['old']
    assert abs(sum(out['by_channel'].values()) - 100.0) < 1e-3


def test_unknown_model_raises():
    with pytest.raises(ValueError):
        attribute([], model='magic')


def test_no_touchpoints_skipped():
    convs = [{'value': 100, 'touchpoints': []}]
    out = attribute(convs, model='last')
    assert out['conversions_attributed'] == 0
    assert out['by_channel'] == {}


def test_aggregates_across_conversions():
    convs = [
        _conv(50, [('a', '2026-01-01T00:00:00')]),
        _conv(30, [('a', '2026-01-02T00:00:00')]),
    ]
    out = attribute(convs, model='last')
    assert out['by_channel'] == {'a': 80.0}
    assert out['top_channel'] == 'a'


def test_invalid_ts_falls_back_to_linear_in_time_decay():
    convs = [_conv(100, [('a', 'bad-ts'), ('b', 'also-bad')])]
    out = attribute(convs, model='time_decay')
    # last ts unparseable → fallback linear
    assert out['by_channel'] == {'a': 50.0, 'b': 50.0}


def test_compare_models_returns_all():
    convs = [_conv(100, [('a', '2026-01-01T00:00:00'),
                          ('b', '2026-01-08T00:00:00')])]
    out = compare_models(convs)
    assert set(out.keys()) == {'last', 'first', 'linear', 'time_decay'}
    assert out['last']['by_channel']['b'] == 100.0
    assert out['first']['by_channel']['a'] == 100.0
