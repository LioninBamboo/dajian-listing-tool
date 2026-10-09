"""SKU 经济数据 (售价/总成本) 双源加载.

供利润感知出价 (cro_margin_aware_bid) 与 offer 定价 (cro_offer_pricing)
共用. 双源, 前者优先 (与 cro_thresholds._sku_category_map /
cro_inventory_filter._load_stock_map 同模式):

1. `products.ourPrice / total_cost` — 旧设计/测试夹具 schema.
2. `collected_products.suggested_price + cost_breakdown.total_dajian_cost`
   — 生产 schema (ebay_collection.db 没有 products 表).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'


def load_sku_economics(sku: str,
                       db_path: Optional[Path] = None) -> Optional[dict]:
    """返回 {'price': float, 'cost': float} 或 None (两个来源都没有)."""
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return None
    with sqlite3.connect(str(db)) as c:
        c.row_factory = sqlite3.Row
        try:
            r = c.execute(
                "SELECT ourPrice, total_cost FROM products WHERE sku=?",
                (sku,),
            ).fetchone()
            if r is not None:
                price = float(r['ourPrice'] or 0)
                cost = float(r['total_cost'] or 0)
                if price > 0 and cost > 0:
                    return {'price': price, 'cost': cost}
        except sqlite3.OperationalError:
            pass
        try:
            r = c.execute(
                "SELECT suggested_price, cost_breakdown "
                "FROM collected_products WHERE sku=?",
                (sku,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    if r is None:
        return None
    try:
        breakdown = json.loads(r['cost_breakdown']) if r['cost_breakdown'] else {}
    except (TypeError, json.JSONDecodeError):
        breakdown = {}
    price = float(r['suggested_price'] or 0)
    cost = float((breakdown or {}).get('total_dajian_cost') or 0)
    if price > 0 and cost > 0:
        return {'price': price, 'cost': cost}
    return None
