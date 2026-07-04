"""利润感知出价的经济数据来源 — 生产 schema 回退路径.

修复前 bid_cap_for_sku 只查 products 表 (仅测试夹具存在), 生产恒退化为
HARD_FLOOR_PCT=5%, S36 从未生效; 高毛利 SKU 的广告出价被压死在 5%,
这也是大量 promote 'already at cap (5%)' 空转的根因之一.
"""
from __future__ import annotations

import json
import sqlite3

from src.services.cro_margin_aware_bid import (
    HARD_FLOOR_PCT, bid_cap_for_sku,
)


def _seed_collected(db, sku, price, cost):
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE IF NOT EXISTS collected_products "
                  "(sku TEXT PRIMARY KEY, suggested_price REAL, "
                  "cost_breakdown TEXT, status TEXT)")
        c.execute("INSERT INTO collected_products VALUES (?,?,?,'PUBLISHED')",
                  (sku, price, json.dumps({'total_dajian_cost': cost})))
        c.commit()


def test_bid_cap_reads_collected_products_on_production_schema(tmp_path):
    db = tmp_path / 'prod.db'
    # 高毛利 (价 400 / 成本 150): cap 必须明显高于 5% 硬地板
    _seed_collected(db, 'HIGH', 400.0, 150.0)
    cap = bid_cap_for_sku('HIGH', db_path=db,
                          margin_calc=lambda p, c: (p - c) / p)
    assert cap > HARD_FLOOR_PCT

    # 微利 (价 100 / 成本 98): 仍压回硬地板
    _seed_collected(db, 'THIN', 100.0, 98.0)
    cap_thin = bid_cap_for_sku('THIN', db_path=db,
                               margin_calc=lambda p, c: (p - c) / p)
    assert cap_thin == HARD_FLOOR_PCT


def test_bid_cap_missing_everything_returns_floor(tmp_path):
    db = tmp_path / 'empty.db'
    sqlite3.connect(str(db)).close()
    assert bid_cap_for_sku('GHOST', db_path=db) == HARD_FLOOR_PCT
