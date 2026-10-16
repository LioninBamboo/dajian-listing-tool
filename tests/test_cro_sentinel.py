"""CRO sentinel + effect audit 测试."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest


def _setup_snapshots(db_path: Path, rows):
    with sqlite3.connect(str(db_path)) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS cro_snapshots (
                snapshot_date TEXT NOT NULL,
                sku TEXT NOT NULL,
                listing_id TEXT,
                impressions INTEGER, views INTEGER,
                transactions INTEGER, sold_qty INTEGER,
                ctr REAL, cvr REAL, str_pct REAL,
                funnel_stage TEXT, cro_score INTEGER,
                top_action TEXT, actions_json TEXT,
                created_at TEXT,
                PRIMARY KEY (snapshot_date, sku)
            )
        """)
        c.executemany(
            "INSERT OR REPLACE INTO cro_snapshots "
            "(snapshot_date, sku, cro_score, funnel_stage) VALUES (?,?,?,?)",
            rows,
        )
        c.commit()


def test_sentinel_no_alert_on_stable(tmp_path):
    db = tmp_path / "test.db"
    today = date.today()
    rows = []
    for i in range(14):
        d = (today - timedelta(days=i)).isoformat()
        for sku in ('A', 'B', 'C'):
            rows.append((d, sku, 70, 'healthy'))
    _setup_snapshots(db, rows)
    from scripts.cro_sentinel import evaluate
    rep = evaluate(db_path=db)
    assert rep['should_alert'] is False
    assert abs(rep['drop']) < 1


def test_sentinel_alerts_on_drop(tmp_path):
    db = tmp_path / "test.db"
    today = date.today()
    rows = []
    # 上 7 天: 80 分; 本 7 天: 65 分 → drop = 15
    for i in range(14):
        d = (today - timedelta(days=i)).isoformat()
        score = 65 if i < 7 else 80
        for sku in ('A', 'B', 'C'):
            rows.append((d, sku, score, 'healthy'))
    _setup_snapshots(db, rows)
    from scripts.cro_sentinel import evaluate
    rep = evaluate(db_path=db)
    assert rep['should_alert'] is True
    assert rep['drop'] >= 5


def test_sentinel_alerts_on_worsened_ratio(tmp_path):
    db = tmp_path / "test.db"
    today = date.today()
    yest = (today - timedelta(days=1)).isoformat()
    today_iso = today.isoformat()
    # 5 SKU 都恶化 (>= 5 分 跌)
    rows = []
    for sku in ('A', 'B', 'C', 'D', 'E'):
        rows.append((yest, sku, 80, 'healthy'))
        rows.append((today_iso, sku, 60, 'low_ctr'))
    _setup_snapshots(db, rows)
    from scripts.cro_sentinel import evaluate
    rep = evaluate(db_path=db)
    assert rep['should_alert'] is True
    assert len(rep['worsened']) == 5


def test_effect_audit_classifies_improvement(tmp_path):
    db = tmp_path / "test.db"
    queue = tmp_path / "q.jsonl"
    today = date.today()
    done_day = today - timedelta(days=10)
    after_day = done_day + timedelta(days=7)
    _setup_snapshots(db, [
        (done_day.isoformat(), 'A', 50, 'low_ctr'),
        (after_day.isoformat(), 'A', 75, 'healthy'),
    ])
    queue.write_text(json.dumps({
        'sku': 'A', 'action': 'price_drop', 'priority': 1,
        'status': 'done',
        'done_at': datetime.combine(done_day, datetime.min.time()).isoformat(),
        'enqueued_at': datetime.combine(done_day, datetime.min.time()).isoformat(),
    }) + '\n', encoding='utf-8')
    from scripts.cro_effect_audit import evaluate_actions
    rep = evaluate_actions(window_days=7, since_days=60,
                            queue_path=queue, db_path=db)
    assert rep['total_evaluated'] == 1
    assert rep['rows'][0]['verdict'] == 'improved'
    assert rep['by_action']['price_drop']['improved'] == 1


def test_effect_audit_skips_when_no_after_snapshot(tmp_path):
    db = tmp_path / "test.db"
    queue = tmp_path / "q.jsonl"
    done_day = date.today() - timedelta(days=2)  # 7d 后还没到
    _setup_snapshots(db, [
        (done_day.isoformat(), 'A', 50, 'low_ctr'),
    ])
    queue.write_text(json.dumps({
        'sku': 'A', 'action': 'price_drop', 'priority': 1, 'status': 'done',
        'done_at': datetime.combine(done_day, datetime.min.time()).isoformat(),
    }) + '\n', encoding='utf-8')
    from scripts.cro_effect_audit import evaluate_actions
    rep = evaluate_actions(window_days=7, queue_path=queue, db_path=db)
    assert rep['total_evaluated'] == 0
