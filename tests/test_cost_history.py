"""P13 — cost_history tests."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.services import cost_history as ch


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / 'ch.db'


def test_record_and_replace_same_day(tmp_db):
    n = ch.record_cost_snapshot(
        [{'sku': 'A', 'total_cost': 20.0}, {'sku': 'B', 'total_cost': 30.0}],
        db_path=tmp_db, date='2026-05-01',
    )
    assert n == 2
    # 重写同日 → REPLACE
    ch.record_cost_snapshot([{'sku': 'A', 'total_cost': 25.0}],
                              db_path=tmp_db, date='2026-05-01')
    a = ch.detect_anomalies(db_path=tmp_db, today='2026-05-01')
    assert a == []  # 同日只有一条, 没有 baseline


def test_detect_anomaly_above_threshold(tmp_db):
    ch.record_cost_snapshot([{'sku': 'X', 'total_cost': 20.0}],
                              db_path=tmp_db, date='2026-04-25')
    ch.record_cost_snapshot([{'sku': 'X', 'total_cost': 25.0}],  # +25%
                              db_path=tmp_db, date='2026-05-01')
    a = ch.detect_anomalies(db_path=tmp_db, today='2026-05-01',
                             jump_pct=0.10, lookback_days=10)
    assert len(a) == 1
    assert a[0]['sku'] == 'X'
    assert a[0]['jump_pct'] == 0.25


def test_detect_no_alarm_below_threshold(tmp_db):
    ch.record_cost_snapshot([{'sku': 'Y', 'total_cost': 20.0}],
                              db_path=tmp_db, date='2026-04-28')
    ch.record_cost_snapshot([{'sku': 'Y', 'total_cost': 21.0}],  # +5%
                              db_path=tmp_db, date='2026-05-01')
    a = ch.detect_anomalies(db_path=tmp_db, today='2026-05-01',
                             jump_pct=0.10, lookback_days=10)
    assert a == []


def test_detect_ignores_decreases(tmp_db):
    ch.record_cost_snapshot([{'sku': 'Z', 'total_cost': 30.0}],
                              db_path=tmp_db, date='2026-04-28')
    ch.record_cost_snapshot([{'sku': 'Z', 'total_cost': 20.0}],
                              db_path=tmp_db, date='2026-05-01')
    a = ch.detect_anomalies(db_path=tmp_db, today='2026-05-01', lookback_days=10)
    assert a == []


def test_skip_invalid_rows(tmp_db):
    n = ch.record_cost_snapshot(
        [{'sku': '', 'total_cost': 10}, {'sku': 'A', 'total_cost': 0},
         {'sku': 'B', 'total_cost': 'abc'}, {'sku': 'C', 'total_cost': 12.5}],
        db_path=tmp_db, date='2026-05-01',
    )
    assert n == 1


def test_alert_html_renders(tmp_db):
    ch.record_cost_snapshot([{'sku': 'A', 'total_cost': 10}],
                              db_path=tmp_db, date='2026-04-25')
    ch.record_cost_snapshot([{'sku': 'A', 'total_cost': 15}],
                              db_path=tmp_db, date='2026-05-01')
    anom = ch.detect_anomalies(db_path=tmp_db, today='2026-05-01', lookback_days=10)
    h = ch.render_alert_html(anom)
    assert '成本异动' in h and '+50' in h
