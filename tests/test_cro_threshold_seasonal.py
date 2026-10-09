"""S28 \u2014 \u5b63\u8282\u6027\u9608\u503c\u8870\u51cf (\u8fd1 7d / \u8001 8-30d \u52a0\u6743)."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from src.services.cro_thresholds import (
    _learn_payload, _seasonal_weighted_median,
    WEIGHT_RECENT_DAYS, WEIGHT_RECENT_RATIO,
)


def _seed(db: Path, rows):
    with sqlite3.connect(str(db)) as c:
        c.executescript("""
            CREATE TABLE products (sku TEXT PRIMARY KEY, dajian_category TEXT);
            CREATE TABLE cro_snapshots (
                sku TEXT, snapshot_date TEXT,
                ctr REAL, cvr REAL, str_pct REAL, impressions INTEGER
            );
        """)
        cats = {sku: cat for sku, cat, *_ in rows}
        for sku, cat in cats.items():
            c.execute("INSERT INTO products VALUES (?,?)", (sku, cat))
        for sku, _cat, snap, ctr, cvr, strv, imp in rows:
            c.execute(
                "INSERT INTO cro_snapshots VALUES (?,?,?,?,?,?)",
                (sku, snap, ctr, cvr, strv, imp),
            )
        c.commit()


def test_weighted_median_two_buckets():
    # \u8fd1 = 0.02, \u8001 = 0.01 \u2192 0.5*0.02 + 0.5*0.01 = 0.015
    out = _seasonal_weighted_median([0.02], [0.01])
    assert abs(out - 0.015) < 1e-9


def test_weighted_median_one_bucket_empty():
    assert _seasonal_weighted_median([0.03, 0.04], []) == 0.035
    assert _seasonal_weighted_median([], [0.01, 0.02]) == 0.015
    assert _seasonal_weighted_median([], []) is None


def test_seasonal_weight_pulls_threshold_toward_recent(tmp_path: Path):
    """\u8001\u6570\u636e CTR \u5f88\u4f4e (\u8282\u540e\u51b7\u5374), \u8fd1\u6570\u636e CTR \u9ad8.
    weighted=True \u4e0b\u9608\u503c\u4e0d\u5e94\u88ab\u8001\u6570\u636e\u62d6\u4f4e\u5230\u4ec5\u8fd1\u671f\u4e2d\u4f4d\u6570\u4ee5\u4e0b."""
    db = tmp_path / 'e.db'
    today = date.today()
    rows = []
    # 8-30d \u533a\u95f4 (\u8001) \u2014\u2014 ctr=0.005 \u4f4e
    for i in range(15):
        d = (today - timedelta(days=10 + (i % 20))).isoformat()
        rows.append((f'OLD-{i}', 'CAT_A', d, 0.005, 0.01, 0.0003, 200))
    # \u8fd1 7d \u2014\u2014 ctr=0.025 \u9ad8 (\u4f8b\u5982\u8282\u65e5\u4fc3\u9500\u62c9\u9ad8)
    for i in range(15):
        d = (today - timedelta(days=i % WEIGHT_RECENT_DAYS)).isoformat()
        rows.append((f'NEW-{i}', 'CAT_A', d, 0.025, 0.02, 0.0010, 200))
    _seed(db, rows)

    weighted = _learn_payload(db, weighted=True)
    flat = _learn_payload(db, weighted=False)

    assert 'CAT_A' in weighted and 'CAT_A' in flat
    # weighted: 0.5*0.025 + 0.5*0.005 = 0.015 ; flat: median \u63a5\u8fd1\u8001\u6570\u636e (15 vs 15 \u4e2d\u4f4d) \u2248 0.005-0.015
    assert weighted['CAT_A']['ctr'] >= 0.014
    # \u52a0\u6743\u540e\u9608\u503c\u660e\u663e\u9ad8\u4e8e\u5e73\u5747\u4e2d\u4f4d\u6570 (\u88ab\u8001\u6570\u636e\u62d6\u4f4e\u7684\u7248\u672c)
    assert weighted['CAT_A']['ctr'] >= flat['CAT_A']['ctr']


def test_constants_exported():
    assert WEIGHT_RECENT_DAYS == 7
    assert 0 < WEIGHT_RECENT_RATIO < 1
