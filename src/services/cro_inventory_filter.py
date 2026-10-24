"""S27 — 库存 × CRO 联动.

在 P1 动作入队前过滤低库存 SKU, 避免对近 OOS 商品调价/加推广 bid
(烧广告预算 × 破坏仅有库存的转化).

注意:
  - eBay 单 listing 数量恒为 1 (batch_publish.py / app.py / server.py 都是 quantity:1).
  - 卖出后 eBay 自动把 qty 置 0, 不会出现并发卖出 dajian_stock 多份的情况.
  - 因此 fulfillment 风险是 supplier 端 (dajian_stock = 0) 时下单后无法履约.
  - MIN_STOCK_FOR_PROMOTE = 1 已足够 (有 1 件即可履约本次 listing 的唯一一笔).

规则:
  - 仅 INVENTORY_GATED_ACTIONS = {price_drop, promote} 受限
  - dajian_stock < MIN_STOCK_FOR_PROMOTE → unsafe
  - dajian_stock 为 None 或 0 → unsafe (保守, 列入 dropped)
  - image_refresh / fill_specifics 不受库存限 (仅优化页面, 不烧预算)
  - 库存表无该 SKU 记录 → 默认保留 (避免冷启动/测试场景全部被拦)
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / 'ebay_collection.db'

# eBay 单刊登 qty 恒为 1, 故 supplier 端有 1 件即可履约本次唯一一笔.
MIN_STOCK_FOR_PROMOTE = 1
INVENTORY_GATED_ACTIONS = frozenset({'price_drop', 'promote'})


def _load_stock_map(db_path: Optional[Path] = None) -> Dict[str, int]:
    """sku → 库存. 双源, 前者优先 (与 cro_thresholds._sku_category_map 同模式):

    1. `products.dajian_stock` — 旧设计/测试夹具 schema.
    2. `collected_products.stock` — 生产 schema (ebay_collection.db 没有
       products 表; 此前 OperationalError 被静默吞掉导致库存过滤在生产
       从未生效, CRO 可能对缺货 SKU 调价/加广告).
    """
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        return {}
    out: Dict[str, int] = {}
    with sqlite3.connect(str(db)) as c:
        try:
            for sku, stock in c.execute(
                "SELECT sku, dajian_stock FROM products"
            ):
                if stock is None:
                    continue
                try:
                    out[str(sku)] = int(stock)
                except (TypeError, ValueError):
                    continue
        except sqlite3.OperationalError:
            pass
        try:
            for sku, stock in c.execute(
                "SELECT sku, stock FROM collected_products"
            ):
                key = str(sku)
                if key in out or stock is None:
                    continue
                try:
                    out[key] = int(stock)
                except (TypeError, ValueError):
                    continue
        except sqlite3.OperationalError:
            pass
    if not out:
        logger.warning(
            "cro_inventory_filter: no stock source available (neither "
            "products.dajian_stock nor collected_products.stock) — "
            "inventory gating is a no-op")
    return out


def is_safe_for_action(sku: str, action: str,
                       stock_map: Dict[str, int],
                       min_stock: int = MIN_STOCK_FOR_PROMOTE) -> bool:
    if action not in INVENTORY_GATED_ACTIONS:
        return True
    s = stock_map.get(str(sku))
    if s is None or s < min_stock:
        return False
    return True


def filter_safe_actions(actions: List[Dict[str, Any]],
                        db_path: Optional[Path] = None,
                        min_stock: int = MIN_STOCK_FOR_PROMOTE,
                        ) -> Dict[str, Any]:
    """\u8fd4\u56de {kept, dropped, dropped_reasons}. dropped_reasons[sku|action] = 'low_stock:N'.

    \u5b89\u5168\u9ed8\u8ba4: \u5e93\u5b58\u8868\u4e3a\u7a7a/\u4e0d\u53ef\u7528 \u2192 \u4e0d\u8fc7\u6ee4 (\u907f\u514d\u6d4b\u8bd5/\u51b7\u542f\u52a8\u573a\u666f\u5168\u90e8\u88ab\u62e6).
    \u4ec5\u5f53 stock_map \u6709\u7c7b\u4f3c SKU \u8bb0\u5f55, \u624d\u5224\u65ad\u8be5 SKU \u5b9e\u9645\u4f4e\u5e93\u5b58 \u2192 drop.
    """
    stock_map = _load_stock_map(db_path)
    if not stock_map:
        return {'kept': list(actions), 'dropped': [], 'dropped_reasons': {}}
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    reasons: Dict[str, str] = {}
    for a in actions:
        sku = a.get('sku')
        act = a.get('action')
        if not sku or not act:
            kept.append(a)
            continue
        # \u5e93\u5b58\u56fe\u4e2d\u6ca1\u6709\u8be5 SKU \u2192 \u9ed8\u8ba4\u4fdd\u7559 (\u907f\u514d\u8bef\u4f24)
        if str(sku) not in stock_map:
            kept.append(a)
            continue
        if is_safe_for_action(str(sku), str(act), stock_map, min_stock=min_stock):
            kept.append(a)
        else:
            dropped.append(a)
            reasons[f"{sku}|{act}"] = f"low_stock:{stock_map.get(str(sku), 0)}"
    return {'kept': kept, 'dropped': dropped, 'dropped_reasons': reasons}
