"""S31 \u2014 A/B lift \u91cf\u5316\u62a5\u8868."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scripts.cro_ab_lift import (
    _bootstrap_ci, compute_ab_lift, render_lift_html,
)


def _seed_snapshots(db: Path, rows):
    with sqlite3.connect(str(db)) as c:
        c.execute("""
            CREATE TABLE cro_snapshots (
                sku TEXT, snapshot_date TEXT, cro_score REAL
            )
        """)
        for r in rows:
            c.execute("INSERT INTO cro_snapshots VALUES (?,?,?)", r)
        c.commit()


def _write_queue(qp: Path, rows):
    qp.parent.mkdir(parents=True, exist_ok=True)
    with qp.open('w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def test_bootstrap_ci_simple():
    lo, hi = _bootstrap_ci([1.0, 2.0, 3.0, 4.0, 5.0], n=200, seed=1)
    assert lo < 3.0 < hi


def test_bootstrap_ci_empty_returns_nan():
    lo, hi = _bootstrap_ci([])
    assert lo != lo and hi != hi  # NaN


def test_compute_ab_lift_treatment_better_than_control(tmp_path: Path):
    db = tmp_path / 'e.db'
    today = date.today()
    snaps = []
    # treatment: \u5165\u961f\u540e cro_score \u4ece 30 \u6da8 \u2192 60 (+30)
    for i in range(8):
        sku = f'T-{i}'
        snaps.append((sku, (today - timedelta(days=10)).isoformat(), 30.0))
        snaps.append((sku, (today - timedelta(days=2)).isoformat(), 60.0))
    # control: cro_score 30 \u2192 32 (\u51e0\u4e4e\u4e0d\u53d8)
    for i in range(8):
        sku = f'C-{i}'
        snaps.append((sku, (today - timedelta(days=10)).isoformat(), 30.0))
        snaps.append((sku, (today - timedelta(days=2)).isoformat(), 32.0))
    _seed_snapshots(db, snaps)

    qp = tmp_path / 'q.jsonl'
    base_anchor = (datetime.now(timezone.utc).replace(tzinfo=None)
                   - timedelta(days=10))
    rows = []
    for i in range(8):
        rows.append({
            'sku': f'T-{i}', 'action': 'image_refresh', 'cohort': 'treatment',
            'status': 'done', 'enqueued_at': base_anchor.isoformat(),
            'done_at': base_anchor.isoformat(),
        })
        rows.append({
            'sku': f'C-{i}', 'action': 'image_refresh', 'cohort': 'control',
            'status': 'pending', 'enqueued_at': base_anchor.isoformat(),
        })
    _write_queue(qp, rows)

    rep = compute_ab_lift(window_days=7, since_days=30,
                          queue_path=qp, db_path=db)
    s = rep['by_action']['image_refresh']
    assert s['treatment_n'] == 8
    assert s['control_n'] == 8
    assert s['lift'] is not None and s['lift'] > 20  # ~28
    assert s['significant']  # CI should not cross 0


def test_compute_ab_lift_marks_insignificant_when_overlap(tmp_path: Path):
    db = tmp_path / 'e.db'
    today = date.today()
    snaps = []
    # treatment 脱节1-9, control 脱节0-8 — 两组重叠, lift ~1
    for prefix, deltas in [('T', list(range(1, 10))),
                            ('C', list(range(0, 9)))]:
        for i, d in enumerate(deltas):
            sku = f'{prefix}-{i}'
            snaps.append((sku, (today - timedelta(days=10)).isoformat(), 40.0))
            snaps.append((sku, (today - timedelta(days=2)).isoformat(), 40.0 + d))
    _seed_snapshots(db, snaps)

    qp = tmp_path / 'q.jsonl'
    base_anchor = (datetime.now(timezone.utc).replace(tzinfo=None)
                   - timedelta(days=10))
    rows = []
    for i in range(9):
        rows.append({'sku': f'T-{i}', 'action': 'promote', 'cohort': 'treatment',
                     'status': 'done', 'enqueued_at': base_anchor.isoformat(),
                     'done_at': base_anchor.isoformat()})
        rows.append({'sku': f'C-{i}', 'action': 'promote', 'cohort': 'control',
                     'status': 'pending', 'enqueued_at': base_anchor.isoformat()})
    _write_queue(qp, rows)

    rep = compute_ab_lift(window_days=7, since_days=30,
                          queue_path=qp, db_path=db)
    s = rep['by_action']['promote']
    assert s['treatment_n'] == 9 and s['control_n'] == 9
    # lift ~1, 接近 0
    assert s['lift'] is not None and abs(s['lift']) < 2


def test_compute_ab_lift_skips_non_ab_actions(tmp_path: Path):
    """price_drop \u4e0d\u5728 AB_ENABLED, \u4e0d\u5e94\u51fa\u73b0\u5728\u62a5\u8868."""
    db = tmp_path / 'e.db'
    _seed_snapshots(db, [])
    qp = tmp_path / 'q.jsonl'
    _write_queue(qp, [
        {'sku': 'X', 'action': 'price_drop', 'cohort': 'na',
         'status': 'done', 'enqueued_at': datetime.now().isoformat(),
         'done_at': datetime.now().isoformat()},
    ])
    rep = compute_ab_lift(queue_path=qp, db_path=db)
    assert 'price_drop' not in rep['by_action']


def test_render_lift_html_has_table_headers():
    rep = {'by_action': {
        'promote': {
            'treatment_n': 5, 'control_n': 5,
            'treatment_mean': 10, 'control_mean': 2,
            'lift': 8, 'ci_low': 3, 'ci_high': 13,
            'significant': True,
        }
    }}
    html = render_lift_html(rep)
    assert '<table' in html
    assert 'lift' in html
    assert '\u2705' in html  # significant marker
