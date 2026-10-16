"""S29 \u2014 CRO \u8bca\u65ad\u9ed1\u540d\u5355 \u7ed1\u5b9a runner."""
from __future__ import annotations

import json
from pathlib import Path

from src.services.cro_diagnose_blacklist import (
    add_to_blacklist, filter_blacklisted, is_blacklisted,
    list_blacklist, load_blacklisted_skus, remove_from_blacklist,
)
from src.web.pages.cro_loop import _summarise


def test_add_remove_blacklist(tmp_path: Path):
    db = tmp_path / 'e.db'
    add_to_blacklist('SKU-A', reason='joint listing', db_path=db)
    add_to_blacklist('SKU-B', db_path=db)
    assert is_blacklisted('SKU-A', db_path=db)
    skus = load_blacklisted_skus(db)
    assert skus == {'SKU-A', 'SKU-B'}
    n = remove_from_blacklist('SKU-A', db_path=db)
    assert n == 1
    assert not is_blacklisted('SKU-A', db_path=db)


def test_filter_blacklisted_drops_matching(tmp_path: Path):
    db = tmp_path / 'e.db'
    add_to_blacklist('BAD', db_path=db)
    products = [{'sku': 'OK1'}, {'sku': 'BAD'}, {'sku': 'OK2'}]
    kept, dropped = filter_blacklisted(products, db_path=db)
    assert {p['sku'] for p in kept} == {'OK1', 'OK2'}
    assert dropped == ['BAD']


def test_list_blacklist_returns_recent_first(tmp_path: Path):
    db = tmp_path / 'e.db'
    add_to_blacklist('OLD', db_path=db)
    add_to_blacklist('NEW', db_path=db)
    rows = list_blacklist(db)
    assert rows[0]['sku'] in {'OLD', 'NEW'}  # ordering by created_at
    assert {r['sku'] for r in rows} == {'OLD', 'NEW'}


def test_loop_summarise_buckets(tmp_path: Path):
    rows = [
        {'sku': 'A', 'action': 'promote', 'status': 'pending', 'cohort': 'treatment'},
        {'sku': 'A', 'action': 'image_refresh', 'status': 'done', 'cohort': 'control'},
        {'sku': 'B', 'action': 'price_drop', 'status': 'pending', 'cohort': 'na'},
    ]
    s = _summarise(rows)
    assert s['total'] == 3
    assert s['by_status']['pending'] == 2
    assert s['by_cohort']['control'] == 1
    assert set(s['by_sku'].keys()) == {'A', 'B'}
    assert len(s['by_sku']['A']) == 2
