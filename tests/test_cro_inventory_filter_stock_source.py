"""库存过滤器的库存来源 — 生产 schema 回退路径.

生产库 (ebay_collection.db) 没有 `products(dajian_stock)` 表; 真实库存在
`collected_products.stock`. 修复前 OperationalError 被静默吞掉, stock map
恒为空, S27 库存×CRO 联动在生产从未过滤过任何动作 (2026-07-03 确认).
"""
from __future__ import annotations

import sqlite3

from src.services.cro_inventory_filter import _load_stock_map, filter_safe_actions


def _seed_collected_products(db_path, rows):
    with sqlite3.connect(str(db_path)) as c:
        c.execute("CREATE TABLE collected_products "
                  "(sku TEXT PRIMARY KEY, stock INTEGER, status TEXT)")
        c.executemany(
            "INSERT INTO collected_products VALUES (?,?,'PUBLISHED')", rows)
        c.commit()


def test_stock_map_falls_back_to_collected_products(tmp_path):
    db = tmp_path / "prod.db"
    _seed_collected_products(db, [('A', 5), ('B', 0), ('C', None)])
    sm = _load_stock_map(db)
    assert sm == {'A': 5, 'B': 0}


def test_products_table_takes_precedence(tmp_path):
    db = tmp_path / "both.db"
    _seed_collected_products(db, [('A', 99), ('B', 99)])
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE products (sku TEXT PRIMARY KEY, dajian_stock INTEGER)")
        c.execute("INSERT INTO products VALUES ('A', 0)")  # dajian 端缺货
        c.commit()
    sm = _load_stock_map(db)
    assert sm['A'] == 0   # products.dajian_stock 优先
    assert sm['B'] == 99  # 回退 collected_products.stock


def test_production_schema_gates_price_drop_and_promote(tmp_path):
    db = tmp_path / "gate.db"
    _seed_collected_products(db, [('OOS', 0), ('OK', 3)])
    actions = [
        {'sku': 'OOS', 'action': 'price_drop'},
        {'sku': 'OOS', 'action': 'promote'},
        {'sku': 'OOS', 'action': 'image_refresh'},   # 页面优化不受库存限
        {'sku': 'OOS', 'action': 'fill_specifics'},
        {'sku': 'OK', 'action': 'promote'},
        {'sku': 'GHOST', 'action': 'promote'},       # 库存表无记录 → 保留
    ]
    res = filter_safe_actions(actions, db_path=db)
    kept = {(a['sku'], a['action']) for a in res['kept']}
    dropped = {(a['sku'], a['action']) for a in res['dropped']}
    assert dropped == {('OOS', 'price_drop'), ('OOS', 'promote')}
    assert ('OOS', 'image_refresh') in kept
    assert ('OOS', 'fill_specifics') in kept
    assert ('OK', 'promote') in kept
    assert ('GHOST', 'promote') in kept
    assert res['dropped_reasons']['OOS|price_drop'] == 'low_stock:0'
