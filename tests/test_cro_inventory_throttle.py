"""S49 — 库存 throttle tests."""
from __future__ import annotations

import json

from src.services.cro_inventory_throttle import (
    days_runway, evaluate, is_throttled, write_alerts,
)


def test_days_runway_zero_sales_is_infinite():
    assert days_runway(5, 0) == float('inf')


def test_days_runway_basic():
    assert days_runway(20, 4) == 5.0


def test_evaluate_flags_low_runway():
    rows = [
        {'sku': 'fast', 'stock': 5, 'daily_sales': 2.0},
        {'sku': 'safe', 'stock': 100, 'daily_sales': 1.0},
    ]
    out = evaluate(rows)
    assert out['throttle_skus'] == ['fast']
    assert out['safe_count'] == 1


def test_evaluate_sorted_by_runway_asc():
    rows = [
        {'sku': 'mid', 'stock': 6, 'daily_sales': 2.0},   # 3 days
        {'sku': 'crit', 'stock': 1, 'daily_sales': 4.0},   # 0.25 days
    ]
    out = evaluate(rows)
    assert out['throttle_detail'][0]['sku'] == 'crit'


def test_zero_sales_never_throttled():
    rows = [{'sku': 'x', 'stock': 1, 'daily_sales': 0}]
    out = evaluate(rows)
    assert out['throttle_skus'] == []
    assert out['safe_count'] == 1


def test_is_throttled_helper():
    report = {'throttle_skus': ['A', 'B']}
    assert is_throttled('A', report)
    assert not is_throttled('C', report)


def test_write_alerts_appends_jsonl(tmp_path):
    rows = [{'sku': 'low', 'stock': 1, 'daily_sales': 5.0,
             'category': 'Kitchen'}]
    out = evaluate(rows)
    p = tmp_path / 'alerts.jsonl'
    n = write_alerts(out, path=p)
    assert n == 1
    line = p.read_text(encoding='utf-8').splitlines()[0]
    parsed = json.loads(line)
    assert parsed['kind'] == 'replenish_alert'
    assert parsed['sku'] == 'low'


def test_write_alerts_empty_no_file(tmp_path):
    p = tmp_path / 'alerts.jsonl'
    n = write_alerts({'replenish_alerts': []}, path=p)
    assert n == 0
    assert not p.exists()
