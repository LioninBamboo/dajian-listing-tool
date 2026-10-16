"""S27 \u2014 \u5e93\u5b58 \u00d7 CRO \u8054\u52a8 \u8fc7\u6ee4."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src.services.cro_inventory_filter import (
    filter_safe_actions, is_safe_for_action,
    INVENTORY_GATED_ACTIONS, MIN_STOCK_FOR_PROMOTE,
)


def _seed_db(db: Path, rows):
    with sqlite3.connect(str(db)) as c:
        c.execute("CREATE TABLE products (sku TEXT PRIMARY KEY, dajian_stock INTEGER)")
        c.executemany("INSERT INTO products (sku, dajian_stock) VALUES (?, ?)", rows)
        c.commit()


def test_is_safe_skips_low_stock_for_gated_actions(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed_db(db, [('ZERO', 0), ('OK', 50), ('NONE', None)])
    from src.services.cro_inventory_filter import _load_stock_map
    sm = _load_stock_map(db)
    assert is_safe_for_action('OK', 'promote', sm)
    # MIN_STOCK_FOR_PROMOTE=1 → 0 件 unsafe, None unsafe
    assert not is_safe_for_action('ZERO', 'promote', sm)
    assert not is_safe_for_action('NONE', 'promote', sm)
    assert not is_safe_for_action('ZERO', 'price_drop', sm)


def test_is_safe_passes_non_gated_actions(tmp_path: Path):
    db = tmp_path / 'e.db'
    _seed_db(db, [('ZERO', 0)])
    from src.services.cro_inventory_filter import _load_stock_map
    sm = _load_stock_map(db)
    # image_refresh / fill_specifics \u4e0d\u53d7\u5e93\u5b58\u9650
    assert is_safe_for_action('ZERO', 'image_refresh', sm)
    assert is_safe_for_action('ZERO', 'fill_specifics', sm)


def test_one_unit_is_enough_because_ebay_qty_is_1(tmp_path: Path):
    """eBay \u5355 listing qty=1, supplier \u67091\u4ef6\u5373\u53ef\u5c65\u7ea6\u552f\u4e00\u4e00\u7b14."""
    db = tmp_path / 'e.db'
    _seed_db(db, [('SOLO', 1)])
    from src.services.cro_inventory_filter import _load_stock_map
    sm = _load_stock_map(db)
    assert is_safe_for_action('SOLO', 'promote', sm)
    assert is_safe_for_action('SOLO', 'price_drop', sm)


def test_filter_safe_actions_drops_low_stock(tmp_path: Path):
    db = tmp_path / 'e.db'
    # C 未出现在 stock_map 中 (库存表不含该 SKU) → 默认保留
    _seed_db(db, [('A', 100), ('B', 0)])
    actions = [
        {'sku': 'A', 'action': 'promote'},
        {'sku': 'B', 'action': 'promote'},
        {'sku': 'B', 'action': 'image_refresh'},  # 不受限 → 保留
    ]
    out = filter_safe_actions(actions, db_path=db)
    kept_skus = [(a['sku'], a['action']) for a in out['kept']]
    assert ('A', 'promote') in kept_skus
    assert ('B', 'image_refresh') in kept_skus
    assert ('B', 'promote') not in kept_skus
    assert len(out['dropped']) == 1
    assert 'low_stock' in out['dropped_reasons']['B|promote']


def test_constants_exported():
    assert 'promote' in INVENTORY_GATED_ACTIONS
    assert 'price_drop' in INVENTORY_GATED_ACTIONS
    assert MIN_STOCK_FOR_PROMOTE == 1  # eBay qty=1, 1 \u4ef6\u5373\u53ef\u5c65\u7ea6
