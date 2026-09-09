"""S116 — listing velocity tests."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from src.services.cro_listing_velocity import (
    DEFAULT_LIMITS, append_log, can_publish, count_window,
    filter_publishable, load_log, remaining_quota,
)


def test_load_missing_file_empty():
    assert load_log('') == []
    assert load_log('does_not_exist.jsonl') == []


def test_append_then_load(tmp_path):
    p = str(tmp_path / 'log.jsonl')
    append_log(p, 'A')
    append_log(p, 'B')
    log = load_log(p)
    assert len(log) == 2
    assert log[0]['sku'] == 'A'


def test_count_window_filters_by_age():
    now = datetime(2026, 5, 1, 12, 0, 0)
    log = [
        {'sku': 'A', 'ts': (now - timedelta(hours=1)).isoformat()},
        {'sku': 'B', 'ts': (now - timedelta(days=2)).isoformat()},
        {'sku': 'C', 'ts': (now - timedelta(days=10)).isoformat()},
    ]
    assert count_window(log, 1, now=now) == 1
    assert count_window(log, 7, now=now) == 2
    assert count_window(log, 30, now=now) == 3


def test_count_ignores_bad_ts():
    log = [{'sku': 'A', 'ts': 'not-a-date'}]
    assert count_window(log, 1) == 0


def test_remaining_quota_full_when_empty():
    rem = remaining_quota([])
    assert rem['per_day'] == DEFAULT_LIMITS['per_day']


def test_can_publish_blocks_when_over():
    now = datetime(2026, 5, 1, 12, 0, 0)
    log = [{'sku': f'X{i}', 'ts': now.isoformat()}
            for i in range(DEFAULT_LIMITS['per_day'])]
    out = can_publish(log, now=now)
    assert out['allowed'] is False
    assert 'per_day' in out['blocked_by']


def test_can_publish_allowed_when_under():
    out = can_publish([])
    assert out['allowed'] is True
    assert out['blocked_by'] == []


def test_filter_publishable_caps_at_remaining(tmp_path):
    p = str(tmp_path / 'log.jsonl')
    # 不 append, log 空 → 全部允许 (但被 default per_day=50 截)
    skus = [f'SKU{i}' for i in range(60)]
    out = filter_publishable(skus, p)
    assert len(out['allowed']) == 50
    assert len(out['deferred']) == 10
    assert out['cap'] == 50


def test_filter_publishable_custom_limits(tmp_path):
    p = str(tmp_path / 'log.jsonl')
    out = filter_publishable(['A', 'B', 'C', 'D'], p,
                              limits={'per_day': 2, 'per_week': 100,
                                      'per_month': 1000})
    assert out['allowed'] == ['A', 'B']
    assert out['deferred'] == ['C', 'D']
