"""S33 \u2014 promote ROI \u95ed\u73af."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scripts.cro_promote_roi import (
    ROI_ROLLBACK_THRESHOLD, compute_promote_roi, render_roi_html,
)


def _seed(db: Path, rows):
    with sqlite3.connect(str(db)) as c:
        c.execute("""
            CREATE TABLE cro_snapshots (
                sku TEXT, snapshot_date TEXT,
                sold_qty INTEGER, ourPrice REAL
            )
        """)
        for r in rows:
            c.execute("INSERT INTO cro_snapshots VALUES (?,?,?,?)", r)
        c.commit()


def _write_queue(qp: Path, rows):
    qp.parent.mkdir(parents=True, exist_ok=True)
    with qp.open('w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def test_compute_roi_high_lift_is_profitable(tmp_path: Path):
    db = tmp_path / 'e.db'
    today = date.today()
    snaps = []
    # before: 5 \u4ef6/d \u00d7 2d = \u603b\u9500\u91cf 10 (snapshots \u662f\u7d2f\u8ba1\u91cf, \u8fd9\u91cc\u5408\u8ba110)
    snaps.append(('SKU-W', (today - timedelta(days=10)).isoformat(), 5, 50.0))
    snaps.append(('SKU-W', (today - timedelta(days=9)).isoformat(), 5, 50.0))
    # after: 30 \u4ef6/d \u00d7 2d = \u603b\u9500\u91cf 60
    snaps.append(('SKU-W', (today - timedelta(days=2)).isoformat(), 30, 50.0))
    snaps.append(('SKU-W', (today - timedelta(days=1)).isoformat(), 30, 50.0))
    _seed(db, snaps)

    qp = tmp_path / 'q.jsonl'
    done_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
    _write_queue(qp, [{
        'sku': 'SKU-W', 'action': 'promote', 'status': 'done',
        'done_at': done_dt.isoformat(),
        'increment': 5.0,
    }])

    rep = compute_promote_roi(window_days=7, since_days=30,
                              queue_path=qp, db_path=db)
    assert rep['evaluated'] == 1
    r = rep['rows'][0]
    assert r['lift_units'] == 50  # 60-10
    # ad_spend = 5% \u00d7 50 \u00d7 60 = 150 ; revenue = 50 \u00d7 50 = 2500 ; ROI \u2248 16.67
    assert r['roi'] is not None and r['roi'] > 5
    assert not rep['rollback_candidates']


def test_compute_roi_negative_flagged_for_rollback(tmp_path: Path):
    db = tmp_path / 'e.db'
    today = date.today()
    snaps = []
    # before: 100/d, after: 105/d (\u63d0\u5347\u5fae\u5f31)
    snaps.append(('SKU-L', (today - timedelta(days=10)).isoformat(), 100, 30.0))
    snaps.append(('SKU-L', (today - timedelta(days=2)).isoformat(), 105, 30.0))
    _seed(db, snaps)

    qp = tmp_path / 'q.jsonl'
    done_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
    _write_queue(qp, [{
        'sku': 'SKU-L', 'action': 'promote', 'status': 'done',
        'done_at': done_dt.isoformat(),
        'increment': 10.0,  # \u52a0\u4e8610% bid
    }])

    rep = compute_promote_roi(window_days=7, since_days=30,
                              queue_path=qp, db_path=db)
    r = rep['rows'][0]
    # lift_units = 5, revenue = 150 ; ad_spend = 10% \u00d7 30 \u00d7 105 = 315 ; ROI \u2248 0.48
    assert r['roi'] is not None and r['roi'] < ROI_ROLLBACK_THRESHOLD
    assert rep['rollback_candidates']
    assert rep['rollback_candidates'][0]['sku'] == 'SKU-L'


def test_compute_roi_skips_no_data_skus(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed(db, [])
    qp = tmp_path / 'q.jsonl'
    done_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
    _write_queue(qp, [{
        'sku': 'NO-DATA', 'action': 'promote', 'status': 'done',
        'done_at': done_dt.isoformat(),
    }])
    rep = compute_promote_roi(queue_path=qp, db_path=db)
    assert rep['evaluated'] == 0


def test_render_roi_html_smoke():
    html = render_roi_html({
        'evaluated': 2,
        'rollback_candidates': [{'sku': 'L'}],
        'rollback_threshold': 1.0,
        'rows': [
            {'sku': 'A', 'before_qty': 5, 'after_qty': 50, 'lift_units': 45,
             'price': 20, 'ad_spend_est': 50, 'lift_revenue': 900, 'roi': 18.0},
            {'sku': 'L', 'before_qty': 100, 'after_qty': 110, 'lift_units': 10,
             'price': 30, 'ad_spend_est': 330, 'lift_revenue': 300, 'roi': 0.91},
        ],
    })
    assert '<table' in html and 'ROI' in html
