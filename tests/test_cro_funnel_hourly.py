"""S34 \u2014 hourly funnel sampler tests."""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from src.services.cro_funnel_hourly import (
    compute_deltas, ensure_schema, load_recent_funnel, sample_funnel,
)


def _seed_snaps(db: Path, rows):
    sd = date.today().isoformat()
    with sqlite3.connect(str(db)) as c:
        c.execute("""
            CREATE TABLE cro_snapshots (
                snapshot_date TEXT, sku TEXT, funnel_stage TEXT
            )
        """)
        for sku, stage in rows:
            c.execute("INSERT INTO cro_snapshots VALUES (?,?,?)",
                      (sd, sku, stage))
        c.commit()


def test_sample_funnel_writes_row(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed_snaps(db, [
        ('A', 'no_impression'), ('B', 'no_impression'),
        ('C', 'low_ctr'), ('D', 'healthy'),
    ])
    rep = sample_funnel(db_path=db, sampled_at='2026-04-01T10:00')
    assert rep['no_impression'] == 2
    assert rep['low_ctr'] == 1
    assert rep['healthy'] == 1
    assert rep['total'] == 4


def test_load_recent_funnel_returns_ordered(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed_snaps(db, [('A', 'no_impression')])
    sample_funnel(db_path=db, sampled_at='2099-12-31T08:00')
    sample_funnel(db_path=db, sampled_at='2099-12-31T10:00')
    rows = load_recent_funnel(hours=999999, db_path=db)
    assert len(rows) >= 2
    assert rows[0]['sampled_at'] <= rows[-1]['sampled_at']


def test_compute_deltas_detects_change():
    rows = [
        {'sampled_at': 't1', 'no_impression': 5, 'low_ctr': 2,
         'low_cvr': 0, 'low_str': 0, 'healthy': 10, 'total': 17},
        {'sampled_at': 't2', 'no_impression': 12, 'low_ctr': 1,
         'low_cvr': 0, 'low_str': 0, 'healthy': 4, 'total': 17},
    ]
    d = compute_deltas(rows)
    assert d['has_delta']
    assert d['delta']['no_impression'] == 7
    assert d['delta']['healthy'] == -6


def test_compute_deltas_single_point_returns_no_delta():
    d = compute_deltas([{'sampled_at': 't', 'no_impression': 1, 'low_ctr': 0,
                         'low_cvr': 0, 'low_str': 0, 'healthy': 0, 'total': 1}])
    assert not d['has_delta']


def test_ensure_schema_idempotent(tmp_path: Path):
    db = tmp_path / 'e.db'
    ensure_schema(db)
    ensure_schema(db)
    with sqlite3.connect(str(db)) as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='cro_funnel_hourly'"
        ).fetchall()
    assert rows
