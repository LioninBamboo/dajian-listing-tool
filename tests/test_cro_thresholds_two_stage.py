"""S21 — 阈值两阶段安全自学习 (learn_to_pending + promote_pending)."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path


def _seed_db(tmp_path: Path) -> Path:
    db = tmp_path / 'cro21.db'
    today = date.today().isoformat()
    with sqlite3.connect(str(db)) as c:
        c.executescript("""
            CREATE TABLE products (sku TEXT PRIMARY KEY, dajian_category TEXT);
            CREATE TABLE cro_snapshots (
                sku TEXT, snapshot_date TEXT, ctr REAL, cvr REAL,
                str_pct REAL, impressions INTEGER
            );
        """)
        for i in range(12):
            sku = f"S{i}"
            c.execute("INSERT INTO products VALUES (?,?)", (sku, "FURN"))
            c.execute(
                "INSERT INTO cro_snapshots VALUES (?,?,?,?,?,?)",
                (sku, today, 0.025, 0.03, 0.001, 500),
            )
        c.commit()
    return db


def test_learn_to_pending_writes_pending_only(tmp_path):
    from src.services.cro_thresholds import (
        learn_to_pending, load_thresholds, load_pending_thresholds,
    )
    db = _seed_db(tmp_path)
    rep = learn_to_pending(db_path=db)
    assert 'FURN' in rep
    pending = load_pending_thresholds(db)
    assert 'FURN' in pending
    # 生产表仍然为空
    prod = load_thresholds(db)
    assert prod == {}


def test_promote_pending_copies_to_production(tmp_path):
    from src.services.cro_thresholds import (
        learn_to_pending, promote_pending, load_thresholds,
    )
    db = _seed_db(tmp_path)
    learn_to_pending(db_path=db)
    n = promote_pending(db_path=db)
    assert n >= 1
    prod = load_thresholds(db)
    assert 'FURN' in prod


def test_promote_thresholds_blocks_on_explosion(tmp_path, monkeypatch):
    from src.services import cro_thresholds as ct
    db = _seed_db(tmp_path)
    ct.learn_to_pending(db_path=db)

    fake_shadow = {
        'safe_to_promote': False,
        'p1_delta': 50,
        'avg_score_delta': -10,
        'explosions': [{'category': 'FURN', 'old_n': 0, 'new_n': 25}],
    }
    monkeypatch.setattr(
        'scripts.cro_threshold_shadow.shadow_compare',
        lambda products, market_data, old_thr, new_thr: fake_shadow,
    )
    rep = ct.promote_thresholds(products=[], market_data={}, db_path=db,
                                report_dir=tmp_path / 'reports')
    assert rep['blocked'] is True
    assert rep['promoted'] == 0
    # 生产表仍然空
    assert ct.load_thresholds(db) == {}
    # 报告写出
    assert Path(rep['report_path']).exists()


def test_promote_thresholds_applies_when_safe(tmp_path, monkeypatch):
    from src.services import cro_thresholds as ct
    db = _seed_db(tmp_path)
    ct.learn_to_pending(db_path=db)
    monkeypatch.setattr(
        'scripts.cro_threshold_shadow.shadow_compare',
        lambda products, market_data, old_thr, new_thr: {
            'safe_to_promote': True, 'p1_delta': 1,
            'avg_score_delta': 0.5, 'explosions': [],
        },
    )
    rep = ct.promote_thresholds(products=[], market_data={}, db_path=db,
                                report_dir=tmp_path / 'reports')
    assert rep['blocked'] is False
    assert rep['promoted'] >= 1
    assert 'FURN' in ct.load_thresholds(db)
