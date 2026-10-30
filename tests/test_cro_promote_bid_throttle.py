"""S82 — promote bid throttle tests."""
from __future__ import annotations

from src.services.cro_promote_bid_throttle import (
    can_change_bid, count_changes_today, filter_throttled, record_bid_change,
)


def test_no_history_zero(tmp_path):
    log = tmp_path / 't.jsonl'
    assert count_changes_today('SKU1', log_path=log) == 0


def test_record_then_count(tmp_path):
    log = tmp_path / 't.jsonl'
    record_bid_change('SKU1', 5.0, log_path=log,
                      ts='2026-05-05T10:00:00+00:00')
    record_bid_change('SKU1', 10.0, log_path=log,
                      ts='2026-05-05T11:00:00+00:00')
    assert count_changes_today('SKU1', log_path=log,
                               today='2026-05-05') == 2


def test_count_isolates_other_sku(tmp_path):
    log = tmp_path / 't.jsonl'
    record_bid_change('SKU1', 5, log_path=log, ts='2026-05-05T10:00:00+00:00')
    record_bid_change('SKU2', 5, log_path=log, ts='2026-05-05T10:00:00+00:00')
    assert count_changes_today('SKU1', log_path=log, today='2026-05-05') == 1


def test_count_isolates_other_day(tmp_path):
    log = tmp_path / 't.jsonl'
    record_bid_change('SKU1', 5, log_path=log, ts='2026-05-04T10:00:00+00:00')
    assert count_changes_today('SKU1', log_path=log, today='2026-05-05') == 0


def test_can_change_under_limit(tmp_path):
    log = tmp_path / 't.jsonl'
    record_bid_change('SKU1', 5, log_path=log, ts='2026-05-05T10:00:00+00:00')
    out = can_change_bid('SKU1', log_path=log, today='2026-05-05')
    assert out['allowed'] is True
    assert out['used_today'] == 1


def test_can_change_at_limit_blocks(tmp_path):
    log = tmp_path / 't.jsonl'
    for i in range(3):
        record_bid_change('SKU1', 5, log_path=log,
                          ts=f'2026-05-05T1{i}:00:00+00:00')
    out = can_change_bid('SKU1', max_per_day=3,
                         log_path=log, today='2026-05-05')
    assert out['allowed'] is False
    assert out['reason'] == 'daily_throttle_exceeded'


def test_corrupt_line_skipped(tmp_path):
    log = tmp_path / 't.jsonl'
    log.write_text('not json\n{"sku":"SKU1","ts":"2026-05-05T10:00:00+00:00"}\n',
                   encoding='utf-8')
    assert count_changes_today('SKU1', log_path=log, today='2026-05-05') == 1


def test_filter_throttled_partition(tmp_path):
    log = tmp_path / 't.jsonl'
    for i in range(3):
        record_bid_change('A', 5, log_path=log,
                          ts=f'2026-05-05T1{i}:00:00+00:00')
    out = filter_throttled(['A', 'B', 'C'], max_per_day=3,
                           log_path=log, today='2026-05-05')
    assert out['allowed'] == ['B', 'C']
    assert out['blocked'] == ['A']
