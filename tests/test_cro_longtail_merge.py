"""S46 — 长尾归并 tests."""
from __future__ import annotations

from src.services.cro_longtail_merge import suggest_merges


def test_merge_group_with_primary_and_longtails():
    rows = [
        {'sku': 'A1', 'asin': 'B0001', 'sold_30d': 25, 'title': 'Main'},
        {'sku': 'A2', 'asin': 'B0001', 'sold_30d': 0, 'title': 'Var2'},
        {'sku': 'A3', 'asin': 'B0001', 'sold_30d': 1, 'title': 'Var3'},
    ]
    out = suggest_merges(rows)
    assert len(out['merge_groups']) == 1
    g = out['merge_groups'][0]
    assert g['primary_sku'] == 'A1'
    assert set(g['longtail_skus']) == {'A2', 'A3'}
    assert g['longtail_count'] == 2


def test_no_primary_means_all_orphan():
    rows = [
        {'sku': 'X1', 'asin': 'B999', 'sold_30d': 0, 'title': 't'},
        {'sku': 'X2', 'asin': 'B999', 'sold_30d': 1, 'title': 't'},
    ]
    out = suggest_merges(rows)
    assert out['merge_groups'] == []
    assert {o['sku'] for o in out['orphan_longtails']} == {'X1', 'X2'}


def test_no_asin_falls_back_to_title_prefix():
    rows = [
        {'sku': 'T1', 'title': 'Coffee Maker Black 12 Cup Drip Brewer Premium',
         'sold_30d': 30},
        {'sku': 'T2', 'title': 'Coffee Maker Black 12 Cup Drip Brewer Mini',
         'sold_30d': 0},
    ]
    out = suggest_merges(rows)
    assert len(out['merge_groups']) == 1
    assert out['merge_groups'][0]['primary_sku'] == 'T1'


def test_short_title_no_asin_yields_orphan():
    rows = [{'sku': 'S1', 'title': 'short', 'sold_30d': 0}]
    out = suggest_merges(rows)
    assert out['merge_groups'] == []
    assert out['orphan_longtails'][0]['reason'] == 'no_key'


def test_groups_sorted_by_longtail_count():
    rows = [
        {'sku': 'a', 'asin': 'X', 'sold_30d': 50},
        {'sku': 'b', 'asin': 'X', 'sold_30d': 0},
        {'sku': 'c', 'asin': 'Y', 'sold_30d': 50},
        {'sku': 'd', 'asin': 'Y', 'sold_30d': 0},
        {'sku': 'e', 'asin': 'Y', 'sold_30d': 1},
    ]
    out = suggest_merges(rows)
    assert out['merge_groups'][0]['key'] == 'asin:Y'
    assert out['merge_groups'][0]['longtail_count'] == 2


def test_total_longtail_counts_orphans():
    rows = [
        {'sku': 'a', 'asin': 'X', 'sold_30d': 5},
        {'sku': 'b', 'asin': 'X', 'sold_30d': 0},
        {'sku': 'orph', 'title': 'short', 'sold_30d': 0},
    ]
    out = suggest_merges(rows)
    assert out['total_longtail'] == 2
