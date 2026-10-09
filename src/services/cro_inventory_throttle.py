"""S49 — 库存×日销联合 throttle.

防 oversell: 当 stock < days_to_runout * daily_sales 时, 暂时禁用 promote/price_drop,
并 enqueue 'replenish_alert' 给采购。

与 S27 cro_inventory_filter 互补: S27 只过滤 0 库存, S49 看消耗速率。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

DAYS_RUNWAY_THROTTLE = 7
ALERT_PATH_DEFAULT = Path('logs/cro_replenish_alerts.jsonl')


def days_runway(stock: int, daily_sales: float) -> float:
    if daily_sales <= 0:
        return float('inf')
    return stock / daily_sales


def evaluate(rows: Iterable[Dict[str, Any]],
             min_runway: int = DAYS_RUNWAY_THROTTLE,
             ) -> Dict[str, Any]:
    """rows: [{sku, stock, daily_sales, category}].

    输出 throttle_skus / replenish_alerts.
    """
    throttle: List[Dict[str, Any]] = []
    safe = 0
    for r in rows:
        stock = r.get('stock', 0) or 0
        daily = r.get('daily_sales', 0) or 0.0
        runway = days_runway(stock, daily)
        if runway < min_runway:
            throttle.append({
                'sku': r.get('sku'),
                'stock': stock,
                'daily_sales': daily,
                'runway_days': round(runway, 2),
                'category': r.get('category'),
            })
        else:
            safe += 1
    throttle.sort(key=lambda x: x['runway_days'])
    return {
        'throttle_skus': [t['sku'] for t in throttle],
        'throttle_detail': throttle,
        'safe_count': safe,
        'replenish_alerts': throttle,  # 采购看的就是 throttle 列表
    }


def write_alerts(report: Dict[str, Any],
                 path: Path = ALERT_PATH_DEFAULT) -> int:
    alerts = report.get('replenish_alerts') or []
    if not alerts:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        for a in alerts:
            f.write(json.dumps({
                'kind': 'replenish_alert', **a,
            }, ensure_ascii=False, sort_keys=True) + '\n')
    return len(alerts)


def is_throttled(sku: str, report: Dict[str, Any]) -> bool:
    return sku in (report.get('throttle_skus') or [])
