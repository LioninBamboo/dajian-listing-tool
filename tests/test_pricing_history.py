"""P9 — pricing_history tests."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.services import pricing_history as ph


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / 'test.db'


def test_record_and_get(tmp_db):
    rows = [{
        'sku': 'SKU-A', 'listing_id': '111', 'live_price': 50.0,
        'total_cost': 20.0, 'current_bid_pct': 5.0,
        'max_safe_ad_rate': 0.42, 'ad_status': 'active',
        'blacklisted': False,
    }]
    n = ph.record_daily_snapshot(rows, db_path=tmp_db, date='2026-05-01')
    assert n == 1
    h = ph.get_history('SKU-A', days=365, db_path=tmp_db)
    assert len(h) == 1
    assert h[0]['live_price'] == 50.0
    assert h[0]['blacklisted'] == 0


def test_replace_same_day(tmp_db):
    ph.record_daily_snapshot([{'sku': 'X', 'live_price': 10.0}],
                              db_path=tmp_db, date='2026-05-01')
    ph.record_daily_snapshot([{'sku': 'X', 'live_price': 12.0}],
                              db_path=tmp_db, date='2026-05-01')
    h = ph.get_history('X', days=365, db_path=tmp_db)
    assert len(h) == 1
    assert h[0]['live_price'] == 12.0


def test_get_history_orders_ascending(tmp_db):
    ph.record_daily_snapshot([{'sku': 'Y', 'live_price': 1.0}],
                              db_path=tmp_db, date='2026-05-03')
    ph.record_daily_snapshot([{'sku': 'Y', 'live_price': 2.0}],
                              db_path=tmp_db, date='2026-05-01')
    h = ph.get_history('Y', days=365, db_path=tmp_db)
    assert [r['date'] for r in h] == ['2026-05-01', '2026-05-03']


def test_skip_rows_without_sku(tmp_db):
    n = ph.record_daily_snapshot([{'live_price': 10}, {'sku': 'Z'}],
                                  db_path=tmp_db, date='2026-05-01')
    assert n == 1


def test_blacklisted_persisted_as_int(tmp_db):
    ph.record_daily_snapshot([{'sku': 'B', 'blacklisted': True}],
                              db_path=tmp_db, date='2026-05-01')
    h = ph.get_history('B', days=10, db_path=tmp_db)
    assert h[0]['blacklisted'] == 1
