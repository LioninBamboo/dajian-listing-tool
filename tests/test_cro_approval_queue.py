"""S37 \u2014 \u7edf\u4e00\u5ba1\u6279\u961f\u5217 tests."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.services.cro_approval_queue import (
    collect_pending, render_digest_text,
)


def _seed_delist(db: Path, sku: str, expired: bool = False, confirmed: bool = False):
    with sqlite3.connect(str(db)) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cro_delist_pending (
                sku TEXT, token TEXT, expires_at TEXT, confirmed_at TEXT
            )
        """)
        exp = (datetime.now() + timedelta(days=-1 if expired else 7)).isoformat()
        c.execute(
            "INSERT INTO cro_delist_pending VALUES (?,?,?,?)",
            (sku, 'tok-' + sku, exp,
             datetime.now().isoformat() if confirmed else None),
        )
        c.commit()


def _seed_thresh(db: Path, cat: str):
    with sqlite3.connect(str(db)) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cro_thresholds_pending (
                category_id TEXT, ctr REAL, cvr REAL, str_pct REAL, samples INTEGER
            )
        """)
        c.execute(
            "INSERT INTO cro_thresholds_pending VALUES (?,?,?,?,?)",
            (cat, 0.02, 0.03, 0.0008, 50),
        )
        c.commit()


def test_collect_pending_aggregates_three_sources(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed_delist(db, 'SKU-D')
    _seed_thresh(db, 'CAT-X')
    fake_roi = lambda: {'rollback_candidates': [
        {'sku': 'SKU-R', 'roi': 0.5, 'lift_units': 1,
         'ad_spend_est': 100, 'done_at': '2026-04-25'},
    ]}
    rep = collect_pending(db_path=db, roi_compute=fake_roi)
    assert rep['total'] == 3
    assert rep['by_kind'] == {'delist': 1, 'threshold_promote': 1,
                              'promote_rollback': 1}


def test_collect_skips_expired_and_confirmed_delist(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed_delist(db, 'EXP', expired=True)
    _seed_delist(db, 'OK', confirmed=True)
    rep = collect_pending(db_path=db, include_threshold=False,
                          include_roi=False)
    assert rep['total'] == 0


def test_collect_handles_missing_db(tmp_path: Path):
    rep = collect_pending(db_path=tmp_path / 'never.db',
                          roi_compute=lambda: {'rollback_candidates': []})
    assert rep['total'] == 0


def test_render_digest_includes_counts(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed_delist(db, 'A')
    rep = collect_pending(db_path=db, include_threshold=False,
                          include_roi=False)
    text = render_digest_text(rep)
    assert '\u5f85\u5ba1\u6279' in text
    assert '[delist] A' in text


def test_collect_swallows_roi_compute_exceptions(tmp_path: Path):
    db = tmp_path / 'e.db'
    def boom():
        raise RuntimeError('roi failure')
    rep = collect_pending(db_path=db, roi_compute=boom)
    assert rep['by_kind']['promote_rollback'] == 0
