"""S35 \u2014 \u591a\u6e90\u4ea4\u53c9\u6821\u9a8c."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from scripts.cro_data_quality import (
    DRIFT_THRESHOLD, _drift, cross_check, filter_data_quality_blocked,
    write_report,
)


def _seed(db: Path, sku: str, days_ago: int, imp: int, views: int, txn: int):
    sd = (date.today() - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(str(db)) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cro_snapshots (
                snapshot_date TEXT, sku TEXT,
                impressions INTEGER, views INTEGER, transactions INTEGER
            )
        """)
        c.execute(
            "INSERT INTO cro_snapshots VALUES (?,?,?,?,?)",
            (sd, sku, imp, views, txn),
        )
        c.commit()


def test_drift_zero_when_equal():
    assert _drift(0.05, 0.05) == 0.0


def test_drift_handles_zero_safely():
    assert _drift(0, 0) == 0.0
    assert _drift(0, 0.1) == 1.0


def test_cross_check_aligned_not_blocked(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed(db, 'OK', 3, imp=1000, views=30, txn=3)

    def fetcher(sku, start, end):
        return {'impressions': 1010, 'ctr': 0.030, 'cvr': 0.10}
    rep = cross_check(['OK'], fetcher, db_path=db)
    assert rep['evaluated'] == 1
    assert not rep['blocked_skus']


def test_cross_check_drifted_sku_blocked(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed(db, 'BAD', 3, imp=100, views=10, txn=2)  # local CTR=10%

    def fetcher(sku, start, end):
        return {'impressions': 1000, 'ctr': 0.01, 'cvr': 0.20}  # remote 1%
    rep = cross_check(['BAD'], fetcher, threshold=DRIFT_THRESHOLD, db_path=db)
    assert 'BAD' in rep['blocked_skus']
    row = rep['rows'][0]
    assert row['worst_drift'] > DRIFT_THRESHOLD


def test_cross_check_fetcher_error_blocks_sku(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed(db, 'X', 1, 100, 5, 1)

    def fetcher(sku, start, end):
        raise RuntimeError('api 500')
    rep = cross_check(['X'], fetcher, db_path=db)
    assert 'X' in rep['blocked_skus']
    assert 'error' in rep['rows'][0]


def test_filter_blocks_actions_for_listed_skus():
    actions = [{'sku': 'A'}, {'sku': 'B'}, {'sku': 'C'}]
    assert filter_data_quality_blocked(actions, []) == actions
    out = filter_data_quality_blocked(actions, ['B'])
    assert [a['sku'] for a in out] == ['A', 'C']


def test_write_report_round_trip(tmp_path: Path):
    p = write_report({'evaluated': 1, 'blocked_skus': ['X']},
                     out=tmp_path / 'r.json')
    assert p.exists()
    assert 'X' in p.read_text(encoding='utf-8')
