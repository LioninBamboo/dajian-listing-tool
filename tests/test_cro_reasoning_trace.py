"""S54 — reasoning trace tests."""
from __future__ import annotations

from src.services.cro_reasoning_trace import (
    attach_trace, attach_traces_bulk, build_trace, has_signal,
    summarize_trace,
)


def test_build_trace_orders_by_priority():
    trace = build_trace({
        'roi': 0.4,
        'external': 'macro_drop',
        'funnel': 'high_view_low_watch',
    })
    assert trace[0].startswith('external:')
    assert trace[1].startswith('funnel:')
    assert trace[2].startswith('roi:')


def test_build_trace_drops_empty_values():
    trace = build_trace({'funnel': '', 'roi': None, 'external': 'x'})
    assert trace == ['external:x']


def test_build_trace_formats_floats():
    trace = build_trace({'roi': 0.456789})
    assert trace == ['roi:0.46']


def test_build_trace_unknown_kind_goes_last():
    trace = build_trace({'mystery': 'X', 'external': 'Y'})
    assert trace[0] == 'external:Y'
    assert trace[-1] == 'mystery:X'


def test_attach_trace_writes_field():
    a = attach_trace({'sku': 'A', 'action': 'price_drop'},
                     {'funnel': 'low_ctr', 'roi': 0.3})
    assert a['reasoning_trace'] == ['funnel:low_ctr', 'roi:0.30']
    assert 'funnel' in a['reasoning_summary']


def test_summarize_trace_truncates():
    long = ['kind:' + ('x' * 30) for _ in range(5)]
    s = summarize_trace(long, max_chars=40)
    assert len(s) <= 40
    assert s.endswith('…')


def test_summarize_empty_trace():
    assert '无显式信号' in summarize_trace([])


def test_attach_traces_bulk_uses_signals_field():
    out = attach_traces_bulk([
        {'sku': 'A', 'signals': {'roi': 0.4}},
        {'sku': 'B', 'signals': {'funnel': 'low_ctr'}},
    ])
    assert out[0]['reasoning_trace'] == ['roi:0.40']
    assert out[1]['reasoning_trace'] == ['funnel:low_ctr']


def test_attach_traces_bulk_lookup_callable():
    out = attach_traces_bulk(
        [{'sku': 'A'}],
        signal_lookup=lambda a: {'external': 'macro_drop'},
    )
    assert out[0]['reasoning_trace'] == ['external:macro_drop']


def test_has_signal_helper():
    a = attach_trace({}, {'funnel': 'low_ctr', 'roi': 0.4})
    assert has_signal(a, 'funnel')
    assert not has_signal(a, 'cohort')
