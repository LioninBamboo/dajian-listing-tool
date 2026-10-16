"""S105 — bundle recommend tests."""
from __future__ import annotations

from src.services.cro_bundle_recommend import (
    find_associations, item_support, pair_counts, recommend_for_sku,
)


def test_item_support_empty():
    assert item_support([]) == {}


def test_item_support_basic():
    txs = [['A', 'B'], ['A', 'C'], ['B'], ['A', 'B']]
    sup = item_support(txs)
    assert sup['A'] == 0.75  # 3/4
    assert sup['B'] == 0.75


def test_pair_counts_dedup_in_tx():
    txs = [['A', 'A', 'B']]  # duplicate A 不算两次
    pc = pair_counts(txs)
    assert pc.get(('A', 'B')) == 1


def test_pair_counts_filters_by_min_item_support():
    txs = [['A', 'B'], ['A', 'C'], ['A', 'D']]
    pc = pair_counts(txs, min_item_support=0.5)  # B/C/D each 33% → drop
    assert pc == {}


def test_find_associations_returns_strong_pairs():
    txs = [
        ['A', 'B'], ['A', 'B'], ['A', 'B'], ['A', 'B'],
        ['A', 'C'], ['B', 'D'], ['E', 'F'],
    ]
    out = find_associations(txs, min_support=0.3, min_confidence=0.5,
                            min_lift=1.0)
    pairs = {(r['antecedent'], r['consequent']) for r in out}
    assert ('A', 'B') in pairs
    assert ('B', 'A') in pairs


def test_find_associations_empty():
    assert find_associations([]) == []


def test_find_associations_filters_below_thresholds():
    txs = [['A', 'B']] * 10 + [['C']] * 90
    out = find_associations(txs, min_support=0.5, min_confidence=0.9,
                            min_lift=1.0)
    assert out == []   # support 仅 10%, 不达标


def test_recommend_for_sku_top_n():
    txs = [['A', 'B']] * 5 + [['A', 'C']] * 5 + [['A', 'D']] * 5
    assoc = find_associations(txs, min_support=0.2, min_confidence=0.2,
                              min_lift=1.0)
    recs = recommend_for_sku('A', assoc, top_n=2)
    assert len(recs) <= 2
    assert all(r['antecedent'] == 'A' for r in recs)


def test_recommend_for_unknown_sku_empty():
    assert recommend_for_sku('ZZZ', []) == []
