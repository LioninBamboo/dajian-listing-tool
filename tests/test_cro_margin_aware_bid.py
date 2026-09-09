"""S36 \u2014 \u5229\u6da6\u611f\u77e5\u51fa\u4ef7."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.services.cro_margin_aware_bid import (
    HARD_CEILING_PCT, HARD_FLOOR_PCT, HIGH_MARGIN_THRESHOLD,
    MIN_MARGIN_FOR_PROMOTE, bid_cap_for_margin, bid_cap_for_sku,
)


def test_low_margin_returns_floor():
    assert bid_cap_for_margin(0.01) == HARD_FLOOR_PCT
    assert bid_cap_for_margin(MIN_MARGIN_FOR_PROMOTE) == HARD_FLOOR_PCT


def test_high_margin_returns_ceiling():
    assert bid_cap_for_margin(0.50) == HARD_CEILING_PCT
    assert bid_cap_for_margin(HIGH_MARGIN_THRESHOLD) == HARD_CEILING_PCT


def test_mid_margin_linear_interpolation():
    mid = (MIN_MARGIN_FOR_PROMOTE + HIGH_MARGIN_THRESHOLD) / 2  # 0.225
    cap = bid_cap_for_margin(mid)
    expected_mid = (HARD_FLOOR_PCT + HARD_CEILING_PCT) / 2
    assert abs(cap - expected_mid) < 0.5


def test_none_margin_treated_as_floor():
    assert bid_cap_for_margin(None) == HARD_FLOOR_PCT


def test_bid_cap_for_sku_missing_db_returns_floor(tmp_path: Path):
    cap = bid_cap_for_sku('NX', db_path=tmp_path / 'no.db')
    assert cap == HARD_FLOOR_PCT


def test_bid_cap_for_sku_uses_high_margin(tmp_path: Path):
    db = tmp_path / 'e.db'
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE products (sku TEXT, ourPrice REAL, total_cost REAL)")
        c.execute("INSERT INTO products VALUES ('HIGH', 100.0, 30.0)")
        c.commit()
    # \u4f20\u5165\u4e00\u4e2a fake margin_calc \u907f\u514d\u4f9d\u8d56 streamlit
    cap = bid_cap_for_sku('HIGH', db_path=db,
                          margin_calc=lambda p, c: (p - c) / p)
    # margin = 0.70 \u2192 cap = HARD_CEILING_PCT
    assert cap == HARD_CEILING_PCT


def test_bid_cap_for_sku_low_margin(tmp_path: Path):
    db = tmp_path / 'e.db'
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE products (sku TEXT, ourPrice REAL, total_cost REAL)")
        c.execute("INSERT INTO products VALUES ('THIN', 100.0, 98.0)")
        c.commit()
    cap = bid_cap_for_sku('THIN', db_path=db,
                          margin_calc=lambda p, c: (p - c) / p)
    assert cap == HARD_FLOOR_PCT
