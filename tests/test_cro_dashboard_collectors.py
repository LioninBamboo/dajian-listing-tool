"""S51 — dashboard collectors tests."""
from __future__ import annotations

import json

from src.services.cro_dashboard_collectors import (
    collect_alerts, collect_approvals, collect_inventory, collect_returns,
)


def _write(p, rows):
    p.write_text(
        '\n'.join(json.dumps(r, sort_keys=True) for r in rows) + '\n',
        encoding='utf-8',
    )


def test_collect_approvals_counts_pending_only(tmp_path):
    p = tmp_path / 'q.jsonl'
    _write(p, [
        {'sku': 'A', 'status': 'pending', 'action': 'price_drop'},
        {'sku': 'B', 'status': 'done', 'action': 'promote'},
        {'sku': 'C', 'status': 'pending', 'action': 'promote'},
    ])
    out = collect_approvals(p)
    assert out['total'] == 2
    assert out['by_action'] == {'price_drop': 1, 'promote': 1}


def test_collect_approvals_missing_file_zero(tmp_path):
    out = collect_approvals(tmp_path / 'nope.jsonl')
    assert out['total'] == 0


def test_collect_alerts_counts_high_priority(tmp_path):
    p = tmp_path / 'a.jsonl'
    _write(p, [
        {'priority': 'high'}, {'priority': 'low'}, {'priority': 'high'},
    ])
    out = collect_alerts(p)
    assert out['high_priority_count'] == 2
    assert out['total'] == 3


def test_collect_returns_filters_by_rate(tmp_path):
    p = tmp_path / 'r.jsonl'
    _write(p, [
        {'sku': 'X', 'return_rate': 0.20},
        {'sku': 'Y', 'return_rate': 0.05},
        {'sku': 'Z', 'return_rate': 0.16},
    ])
    out = collect_returns(p)
    assert out['high_return_count'] == 2


def test_collect_inventory_extracts_skus(tmp_path):
    p = tmp_path / 'inv.jsonl'
    _write(p, [{'sku': 'A'}, {'sku': 'B'}, {'note': 'no sku'}])
    out = collect_inventory(p)
    assert set(out['throttle_skus']) == {'A', 'B'}


def test_collectors_skip_corrupt_lines(tmp_path):
    p = tmp_path / 'bad.jsonl'
    p.write_text('{"sku": "ok"}\nNOT_JSON\n', encoding='utf-8')
    out = collect_inventory(p)
    assert out['throttle_skus'] == ['ok']
