"""S16 — CRO per-category threshold learning + diagnose injection."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _seed_snapshots_and_products(db_path: Path):
    with sqlite3.connect(str(db_path)) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                sku TEXT UNIQUE,
                dajian_category TEXT
            );
            CREATE TABLE IF NOT EXISTS cro_snapshots (
                snapshot_date TEXT, sku TEXT,
                ctr REAL, cvr REAL, str_pct REAL, impressions INTEGER,
                cro_score INTEGER, funnel_stage TEXT,
                PRIMARY KEY (snapshot_date, sku)
            );
        """)
        # 2 个品类 (FURN, TOOL); 每类 12 条 SKU + 高 impressions
        from datetime import date
        today = date.today().isoformat()
        for i in range(12):
            sku_f = f"F{i:02d}"
            sku_t = f"T{i:02d}"
            c.execute("INSERT OR REPLACE INTO products VALUES (?,?,?)",
                      (i + 1, sku_f, 'FURN'))
            c.execute("INSERT OR REPLACE INTO products VALUES (?,?,?)",
                      (i + 100, sku_t, 'TOOL'))
            # FURN: CTR 中位 0.008 (低水位), CVR 0.01
            c.execute("INSERT OR REPLACE INTO cro_snapshots "
                      "(snapshot_date, sku, ctr, cvr, str_pct, impressions, cro_score, funnel_stage) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (today, sku_f, 0.008, 0.01, 0.00008, 500, 60, 'low_ctr'))
            # TOOL: CTR 中位 0.05 (高水位), CVR 0.06
            c.execute("INSERT OR REPLACE INTO cro_snapshots "
                      "(snapshot_date, sku, ctr, cvr, str_pct, impressions, cro_score, funnel_stage) "
                      "VALUES (?,?,?,?,?,?,?,?)",
                      (today, sku_t, 0.05, 0.06, 0.003, 500, 90, 'healthy'))
        c.commit()


def test_learn_thresholds_per_category(tmp_path):
    db = tmp_path / "test.db"
    _seed_snapshots_and_products(db)
    from src.services.cro_thresholds import learn_all, load_thresholds
    learned = learn_all(db_path=db, min_samples=10)
    assert 'FURN' in learned
    assert 'TOOL' in learned
    assert learned['FURN']['ctr'] < learned['TOOL']['ctr']
    loaded = load_thresholds(db_path=db)
    assert loaded['FURN']['cvr'] == pytest.approx(learned['FURN']['cvr'])


def test_diagnose_uses_per_category_thresholds():
    """同一组 metrics 在不同品类阈值下被分到不同 funnel_stage."""
    from src.services.conversion_diagnoser import diagnose_batch
    products = [
        {'sku': 'A', 'listing_id': '1', 'categoryId': 'STRICT',
         'impressions': 200, 'views': 5, 'transactions': 1, 'sold_qty': 1,
         'selling_price': 50, 'age_days': 30, 'title': 't' * 70, 'images': [1, 2]},
    ]
    # 默认 (CTR 1.5%): 5/200 = 2.5% → healthy
    diags_default = diagnose_batch(products)
    assert diags_default[0].funnel_stage == 'healthy'

    # 严格 (CTR 5%): 2.5% < 5% → low_ctr
    diags_strict = diagnose_batch(
        products,
        thresholds_by_category={'STRICT': {'ctr': 0.05, 'cvr': 0.02, 'str': 0.0005}},
    )
    assert diags_strict[0].funnel_stage == 'low_ctr'


def test_load_thresholds_returns_empty_when_table_missing(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    from src.services.cro_thresholds import load_thresholds
    assert load_thresholds(db_path=db) == {}
