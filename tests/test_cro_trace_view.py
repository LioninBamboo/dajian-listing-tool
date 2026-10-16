"""S63 — cro_trace_view tests."""
from __future__ import annotations

from src.services.cro_trace_view import (
    filter_by_kind, group_by_kind, render_chips_html, severity_color,
)


def test_render_chips_html_empty():
    out = render_chips_html([])
    assert '无显式信号' in out


def test_render_chips_html_basic_chip():
    out = render_chips_html(['external:macro_drop'])
    assert '<b>external</b>:macro_drop' in out
    assert 'background:#fee2e2' in out


def test_render_chips_html_unknown_kind_default_color():
    out = render_chips_html(['weird:thing'])
    assert '<b>weird</b>:thing' in out
    assert 'background:#f1f5f9' in out


def test_render_chips_html_no_colon():
    out = render_chips_html(['rawvalue'])
    assert '<b>other</b>:rawvalue' in out


def test_render_chips_html_escapes():
    out = render_chips_html(['external:<script>'])
    assert '<script>' not in out
    assert '&lt;script&gt;' in out


def test_group_by_kind():
    out = group_by_kind(['external:a', 'external:b', 'funnel:c', 'raw'])
    assert out['external'] == ['a', 'b']
    assert out['funnel'] == ['c']
    assert out['other'] == ['raw']


def test_severity_color_external_red():
    assert severity_color(['external:x']) == '#ef4444'


def test_severity_color_funnel_yellow():
    assert severity_color(['funnel:low_ctr']) == '#facc15'


def test_severity_color_returns_yellow():
    assert severity_color(['returns:defective']) == '#facc15'


def test_severity_color_normal_green():
    assert severity_color(['inventory:ok']) == '#22c55e'


def test_severity_color_empty_gray():
    assert severity_color([]) == '#9ca3af'


def test_filter_by_kind_none_returns_all():
    rows = [{'reasoning_trace': ['x:1']}, {'reasoning_trace': []}]
    assert filter_by_kind(rows, None) == rows


def test_filter_by_kind_matches():
    rows = [
        {'sku': 'A', 'reasoning_trace': ['external:a']},
        {'sku': 'B', 'reasoning_trace': ['funnel:b']},
        {'sku': 'C', 'reasoning_trace': []},
    ]
    out = filter_by_kind(rows, 'external')
    assert [r['sku'] for r in out] == ['A']


def test_filter_by_kind_missing_field():
    rows = [{'sku': 'A'}]
    assert filter_by_kind(rows, 'external') == []
