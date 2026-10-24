"""S22 — monthly_report 融合 effect_audit + 阈值反哺."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


def _seed(tmp_path: Path):
    db = tmp_path / 'm.db'
    queue = tmp_path / 'q.jsonl'
    today = date.today()
    with sqlite3.connect(str(db)) as c:
        c.executescript("""
            CREATE TABLE products (sku TEXT PRIMARY KEY, dajian_category TEXT);
            CREATE TABLE cro_snapshots (
                sku TEXT, snapshot_date TEXT, ctr REAL, cvr REAL,
                str_pct REAL, impressions INTEGER, cro_score REAL,
                funnel_stage TEXT
            );
            CREATE TABLE cro_thresholds (
                category_id TEXT PRIMARY KEY, healthy_ctr REAL,
                healthy_cvr REAL, healthy_str REAL,
                sample_size INTEGER, learned_at TEXT
            );
        """)
        c.execute("INSERT INTO cro_thresholds VALUES "
                  "('FURN', 0.02, 0.025, 0.0008, 30, ?)", (today.isoformat(),))
        # 5 SKUs with done price_drop, all worsened → relax suggestion
        for i in range(5):
            sku = f"P{i}"
            c.execute("INSERT INTO products VALUES (?,?)", (sku, "FURN"))
            done_day = today - timedelta(days=15)
            c.execute(
                "INSERT INTO cro_snapshots VALUES (?,?,?,?,?,?,?,?)",
                (sku, done_day.isoformat(), 0.005, 0.01, 0.0001, 200, 60, 'low_ctr'),
            )
            after_day = today - timedelta(days=9)
            c.execute(
                "INSERT INTO cro_snapshots VALUES (?,?,?,?,?,?,?,?)",
                (sku, after_day.isoformat(), 0.004, 0.008, 0.0001, 200, 40, 'low_ctr'),
            )
        c.commit()

    rows = []
    for i in range(5):
        sku = f"P{i}"
        done_dt = datetime.combine(today - timedelta(days=15),
                                   datetime.min.time(),
                                   tzinfo=timezone.utc) + timedelta(hours=10)
        rows.append({
            'sku': sku, 'action': 'price_drop',
            'priority': 1, 'status': 'done',
            'enqueued_at': done_dt.isoformat(),
            'done_at': done_dt.isoformat(),
        })
    with queue.open('w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')
    return db, queue


def test_evaluate_actions_includes_category(tmp_path):
    from scripts.cro_effect_audit import evaluate_actions
    db, q = _seed(tmp_path)
    rep = evaluate_actions(window_days=7, since_days=60,
                           queue_path=q, db_path=db)
    assert rep['total_evaluated'] == 5
    assert all(r.get('category') == 'FURN' for r in rep['rows'])


def test_monthly_report_fuses_audit_and_suggestions(tmp_path):
    from scripts.cro_effect_audit import monthly_report
    db, q = _seed(tmp_path)
    rep = monthly_report(window_days=7, since_days=35,
                         db_path=db, queue_path=q)
    assert rep['total_evaluated'] == 5
    assert 'FURN' in rep['by_category']
    # 5 worsened → improved_rate = 0 → relax suggestion
    assert any(s['direction'] == 'relax' and s['category_id'] == 'FURN'
               for s in rep['suggestions'])
    assert '<h2>' in rep['html'] and 'FURN' in rep['html']
