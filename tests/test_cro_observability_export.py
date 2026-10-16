"""S120 — observability export tests."""
from __future__ import annotations

import pytest

from src.services.cro_observability_export import (
    MetricsRegistry, format_metric_block, format_metric_line,
)


def test_format_line_no_labels():
    assert format_metric_line('m', 1.5) == 'm 1.5'


def test_format_line_with_labels_sorted():
    line = format_metric_line('m', 2, {'b': 'B', 'a': 'A'})
    assert line == 'm{a="A",b="B"} 2.0'


def test_format_line_escapes_special_chars():
    line = format_metric_line('m', 1, {'k': 'a"b\\c\nd'})
    assert 'a\\"b\\\\c\\nd' in line


def test_format_block_invalid_type_raises():
    with pytest.raises(ValueError):
        format_metric_block('m', 'h', 'wrong', [])


def test_format_block_basic():
    block = format_metric_block('http_requests_total', 'help', 'counter',
                                  [({'code': '200'}, 5), ({'code': '500'}, 1)])
    assert '# TYPE http_requests_total counter' in block
    assert 'http_requests_total{code="200"} 5.0' in block


def test_registry_inc_counter():
    r = MetricsRegistry()
    r.inc_counter('reqs', labels={'code': '200'})
    r.inc_counter('reqs', amount=2, labels={'code': '200'})
    assert r.get('reqs', {'code': '200'}) == 3.0


def test_registry_counter_negative_raises():
    r = MetricsRegistry()
    with pytest.raises(ValueError):
        r.inc_counter('m', amount=-1)


def test_registry_set_gauge_overwrites():
    r = MetricsRegistry()
    r.set_gauge('temp', 1.0)
    r.set_gauge('temp', 2.0)
    assert r.get('temp') == 2.0


def test_registry_render_includes_help_and_type():
    r = MetricsRegistry()
    r.set_gauge('queue_depth', 42, help_text='Pending items')
    txt = r.render()
    assert '# HELP queue_depth Pending items' in txt
    assert '# TYPE queue_depth gauge' in txt
    assert 'queue_depth 42.0' in txt


def test_registry_clear():
    r = MetricsRegistry()
    r.inc_counter('m')
    r.clear()
    assert r.render() == ''


def test_registry_get_unknown_returns_zero():
    r = MetricsRegistry()
    assert r.get('not_there') == 0.0
