"""阈值学习的 sku→category 来源 — 生产 schema 回退路径.

生产库 (ebay_collection.db) 没有 `products(dajian_category)` 表; 类目在
`collected_products.optimization` JSON 的 `categoryId` 里. 学习器必须能
从这条生产路径取键, 且键要与消费侧 (competition_monitor 传给
diagnose_batch 的 optimization.categoryId) 一致 — 否则学到的阈值永远
匹配不上, learned_categories 恒为 0 (2026-07-03 生产事故).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path


def _seed_production_schema(db_path: Path, n_per_cat: int = 12):
    """只有 collected_products + cro_snapshots, 没有 products 表."""
    today = date.today().isoformat()
    with sqlite3.connect(str(db_path)) as c:
        c.executescript("""
            CREATE TABLE collected_products (
                sku TEXT PRIMARY KEY, optimization TEXT, status TEXT
            );
            CREATE TABLE cro_snapshots (
                snapshot_date TEXT, sku TEXT,
                ctr REAL, cvr REAL, str_pct REAL, impressions INTEGER,
                cro_score INTEGER, funnel_stage TEXT,
                PRIMARY KEY (snapshot_date, sku)
            );
        """)
        for i in range(n_per_cat):
            sku_a = f"A{i:02d}"
            sku_b = f"B{i:02d}"
            c.execute("INSERT INTO collected_products VALUES (?,?,?)",
                      (sku_a, json.dumps({'categoryId': '38208'}), 'PUBLISHED'))
            c.execute("INSERT INTO collected_products VALUES (?,?,?)",
                      (sku_b, json.dumps({'categoryId': '20487'}), 'PUBLISHED'))
            c.execute("INSERT INTO cro_snapshots VALUES (?,?,?,?,?,?,?,?)",
                      (today, sku_a, 0.008, 0.01, 0.00008, 500, 60, 'low_ctr'))
            c.execute("INSERT INTO cro_snapshots VALUES (?,?,?,?,?,?,?,?)",
                      (today, sku_b, 0.05, 0.06, 0.003, 500, 90, 'healthy'))
        c.commit()


def test_learn_from_collected_products_optimization(tmp_path):
    db = tmp_path / "prod_schema.db"
    _seed_production_schema(db)
    from src.services.cro_thresholds import learn_to_pending, load_pending_thresholds
    learned = learn_to_pending(db_path=db, min_samples=10)
    assert set(learned) == {'38208', '20487'}
    assert learned['38208']['ctr'] < learned['20487']['ctr']
    pending = load_pending_thresholds(db_path=db)
    assert set(pending) == {'38208', '20487'}


def test_products_table_takes_precedence_over_optimization(tmp_path):
    db = tmp_path / "both.db"
    _seed_production_schema(db)
    today = date.today().isoformat()
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE products (sku TEXT PRIMARY KEY, dajian_category TEXT)")
        # A 系列 SKU 同时出现在 products 表 → dajian_category 键优先
        for i in range(12):
            c.execute("INSERT INTO products VALUES (?,?)", (f"A{i:02d}", 'FURN'))
        c.commit()
    from src.services.cro_thresholds import _learn_payload
    learned = _learn_payload(db_path=db, min_samples=10)
    assert 'FURN' in learned
    assert '38208' not in learned      # A 系列被 products 表接管
    assert '20487' in learned          # B 系列仍走 optimization 回退


def test_sku_without_category_is_excluded(tmp_path):
    db = tmp_path / "nocat.db"
    _seed_production_schema(db, n_per_cat=12)
    today = date.today().isoformat()
    with sqlite3.connect(str(db)) as c:
        # 无类目 SKU: optimization 缺 categoryId / 值为 '?'
        c.execute("INSERT INTO collected_products VALUES (?,?,?)",
                  ('NOCAT', json.dumps({'title': 'x'}), 'PUBLISHED'))
        c.execute("INSERT INTO collected_products VALUES (?,?,?)",
                  ('QMARK', json.dumps({'categoryId': '?'}), 'PUBLISHED'))
        for sku in ('NOCAT', 'QMARK'):
            c.execute("INSERT INTO cro_snapshots VALUES (?,?,?,?,?,?,?,?)",
                      (today, sku, 0.09, 0.09, 0.005, 900, 95, 'healthy'))
        c.commit()
    from src.services.cro_thresholds import _learn_payload
    learned = _learn_payload(db_path=db, min_samples=10)
    assert set(learned) == {'38208', '20487'}


def test_min_samples_enforced_on_production_schema(tmp_path):
    db = tmp_path / "small.db"
    _seed_production_schema(db, n_per_cat=5)  # 每类只有 5 条 < min 10
    from src.services.cro_thresholds import _learn_payload
    assert _learn_payload(db_path=db, min_samples=10) == {}
