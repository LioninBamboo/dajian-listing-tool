"""S129 — dry-run replay harness tests."""
from __future__ import annotations

import pytest

from src.services.cro_dryrun_replay_harness import (
    RecordingHarness, build_replay_fetcher, load_events,
)


def test_record_path_required():
    with pytest.raises(ValueError):
        RecordingHarness('')


def test_wrap_fetcher_records_result(tmp_path):
    p = str(tmp_path / 'rec.jsonl')
    h = RecordingHarness(p)
    f = h.wrap_fetcher('get_price', lambda sku: 9.99)
    assert f('A') == 9.99
    events = load_events(p)
    assert len(events) == 1
    assert events[0]['result'] == 9.99
    assert events[0]['kind'] == 'fetcher'


def test_wrap_fetcher_records_exception(tmp_path):
    p = str(tmp_path / 'rec.jsonl')
    h = RecordingHarness(p)
    def bad(_):
        raise RuntimeError('x')
    f = h.wrap_fetcher('boom', bad)
    with pytest.raises(RuntimeError):
        f('A')
    events = load_events(p)
    assert events[0].get('error')


def test_wrap_writer_marks_side_effect(tmp_path):
    p = str(tmp_path / 'rec.jsonl')
    h = RecordingHarness(p)
    written = []
    w = h.wrap_writer('write_sku', lambda r: written.append(r))
    w({'sku': 'A'})
    events = load_events(p)
    assert events[0]['kind'] == 'writer'
    assert events[0]['side_effect'] is True
    assert written == [{'sku': 'A'}]


def test_replay_fetcher_returns_recorded(tmp_path):
    p = str(tmp_path / 'rec.jsonl')
    h = RecordingHarness(p)
    f = h.wrap_fetcher('px', lambda sku: 10 if sku == 'A' else 20)
    f('A'); f('B')
    events = load_events(p)
    rf = build_replay_fetcher(events, 'px')
    # 精确匹配按 sku 召回
    assert rf('A') == 10
    assert rf('B') == 20


def test_replay_fetcher_falls_back_to_order(tmp_path):
    p = str(tmp_path / 'rec.jsonl')
    h = RecordingHarness(p)
    f = h.wrap_fetcher('px', lambda *a, **kw: 7)
    f('A')
    events = load_events(p)
    rf = build_replay_fetcher(events, 'px')
    # 调用与录制 args 不同 → 顺序回放
    assert rf('Z') == 7


def test_replay_fetcher_raises_when_exhausted(tmp_path):
    p = str(tmp_path / 'rec.jsonl')
    h = RecordingHarness(p)
    f = h.wrap_fetcher('px', lambda *a, **kw: 1)
    f('A')
    events = load_events(p)
    rf = build_replay_fetcher(events, 'px')
    rf('A')
    with pytest.raises(LookupError):
        rf('A')


def test_load_events_missing_file_returns_empty(tmp_path):
    assert load_events(str(tmp_path / 'nope.jsonl')) == []


def test_load_events_skips_corrupt_lines(tmp_path):
    p = tmp_path / 'rec.jsonl'
    p.write_text('{"kind":"fetcher"}\nNOT JSON\n{"kind":"writer"}\n')
    events = load_events(str(p))
    assert len(events) == 2


def test_replay_fetcher_only_picks_matching_name(tmp_path):
    p = str(tmp_path / 'rec.jsonl')
    h = RecordingHarness(p)
    h.wrap_fetcher('a', lambda: 1)()
    h.wrap_fetcher('b', lambda: 2)()
    events = load_events(p)
    rf_b = build_replay_fetcher(events, 'b')
    assert rf_b() == 2
