"""S26 — A/B cohort \u5206\u914d + \u961f\u5217\u8fc7\u6ee4."""
from __future__ import annotations

import json
from pathlib import Path

from src.services.cro_ab import assign_cohort, AB_ENABLED_ACTIONS
from src.services.cro_action_queue import enqueue, load_pending


def test_assign_cohort_deterministic():
    a = assign_cohort('SKU-1', 'image_refresh')
    b = assign_cohort('SKU-1', 'image_refresh')
    assert a == b
    assert a in {'control', 'treatment'}


def test_assign_cohort_skip_for_price_drop():
    assert assign_cohort('SKU-X', 'price_drop') == 'na'
    assert assign_cohort('SKU-X', 'fill_specifics') == 'na'


def test_assign_cohort_ratio_within_bounds():
    treats = controls = 0
    for i in range(2000):
        c = assign_cohort(f'SKU-{i}', 'promote')
        if c == 'control':
            controls += 1
        elif c == 'treatment':
            treats += 1
    # 20% \u00b13% \u5bb9\u5dee
    ratio = controls / (treats + controls)
    assert 0.17 <= ratio <= 0.23, ratio


def test_load_pending_excludes_control(tmp_path: Path):
    qp = tmp_path / 'q.jsonl'
    enqueue([
        {'sku': 'A', 'action': 'image_refresh', 'cohort': 'control'},
        {'sku': 'B', 'action': 'image_refresh', 'cohort': 'treatment'},
        {'sku': 'C', 'action': 'price_drop'},
    ], queue_path=qp)
    visible = load_pending(queue_path=qp)
    skus = {r['sku'] for r in visible}
    assert 'A' not in skus  # control \u9690\u85cf
    assert 'B' in skus
    assert 'C' in skus
    full = load_pending(queue_path=qp, include_control=True)
    assert {r['sku'] for r in full} == {'A', 'B', 'C'}


def test_enqueue_auto_assigns_cohort_for_ab_actions(tmp_path: Path):
    qp = tmp_path / 'q.jsonl'
    enqueue([{'sku': 'AUTO-1', 'action': 'promote'}], queue_path=qp)
    rows = []
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            rows.append(json.loads(line))
    assert rows[0]['cohort'] in {'control', 'treatment'}
    assert 'promote' in AB_ENABLED_ACTIONS
