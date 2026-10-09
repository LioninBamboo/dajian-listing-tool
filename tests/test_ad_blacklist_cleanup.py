"""P7 — ad_blacklist_cleanup tests."""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import ad_blacklist_cleanup as cu
from src.services import ad_blacklist as bl


@pytest.fixture
def tmp_bl(tmp_path, monkeypatch):
    p = tmp_path / 'bl.json'
    monkeypatch.setattr(bl, 'DEFAULT_PATH', p)
    monkeypatch.setattr(cu, 'LOG_DIR', tmp_path)
    return p


def test_evaluate_recovery_safe(tmp_bl):
    bl.add_manual('SKU-A', note='manual-test')  # manual won't be touched
    # auto entry: bump 3 times to upgrade reason
    for _ in range(3):
        bl.bump_off_count('SKU-B')
    entry = bl.list_all()['SKU-B']
    res = cu.evaluate_recovery('SKU-B', entry, live_price=50.0, cost=20.0)
    assert res['is_safe'] is True
    assert res['reason'] == 'recovered'
    assert res['max_ad_rate'] >= 0.05


def test_evaluate_recovery_unsafe(tmp_bl):
    for _ in range(3):
        bl.bump_off_count('SKU-X')
    entry = bl.list_all()['SKU-X']
    # high cost relative to price → max_ad_rate negative or tiny
    res = cu.evaluate_recovery('SKU-X', entry, live_price=10.0, cost=9.0)
    assert res['is_safe'] is False
    assert res['reason'] == 'still_unsafe'


def test_evaluate_recovery_no_data(tmp_bl):
    for _ in range(3):
        bl.bump_off_count('SKU-Y')
    entry = bl.list_all()['SKU-Y']
    res = cu.evaluate_recovery('SKU-Y', entry, live_price=None, cost=20.0)
    assert res['is_safe'] is None
    assert res['reason'] == 'no_data'


def test_run_dry_skips_writes(tmp_bl, monkeypatch):
    for _ in range(3):
        bl.bump_off_count('SKU-D')
    # stub real client + cost
    monkeypatch.setattr(cu, '_fetch_cost', lambda sku, db_path=None: 20.0)
    monkeypatch.setattr(cu, '_fetch_live_price', lambda sku, c: 50.0)

    class FakeClient:
        pass
    monkeypatch.setattr(cu, '_make_real_client', lambda: FakeClient())
    r = cu.run(apply=False)
    assert r['summary']['total'] == 1
    # not applied → safe_streak unchanged
    assert bl.list_all()['SKU-D'].get('safe_streak', 0) == 0


def test_run_apply_eventually_removes(tmp_bl, monkeypatch):
    for _ in range(3):
        bl.bump_off_count('SKU-R')
    monkeypatch.setattr(cu, '_fetch_cost', lambda sku, db_path=None: 20.0)
    monkeypatch.setattr(cu, '_fetch_live_price', lambda sku, c: 50.0)

    class FakeClient:
        pass
    monkeypatch.setattr(cu, '_make_real_client', lambda: FakeClient())
    # threshold=2 to make it fast
    cu.run(apply=True, threshold_days=2)
    cu.run(apply=True, threshold_days=2)
    # second run should remove
    assert 'SKU-R' not in bl.list_all()


def test_manual_entries_never_removed(tmp_bl, monkeypatch):
    bl.add_manual('SKU-M', note='hold')
    monkeypatch.setattr(cu, '_fetch_cost', lambda sku, db_path=None: 20.0)
    monkeypatch.setattr(cu, '_fetch_live_price', lambda sku, c: 50.0)

    class FakeClient:
        pass
    monkeypatch.setattr(cu, '_make_real_client', lambda: FakeClient())
    for _ in range(5):
        cu.run(apply=True, threshold_days=1)
    assert 'SKU-M' in bl.list_all()
    assert bl.list_all()['SKU-M']['reason'] == 'manual'
