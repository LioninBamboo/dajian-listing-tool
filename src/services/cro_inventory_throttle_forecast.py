"""S72 — inventory_throttle + forecast 桥接.

不修改 cro_inventory_throttle.py; 提供一个 evaluator 用 cro_sales_forecast 的
days_to_stockout 替代固定均值 days_runway 计算.

输入行格式: {sku, stock, history: [daily_sales,...]}
输出与 throttle.evaluate 兼容: {throttle_skus, details}
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from src.services.cro_sales_forecast import (
    days_to_stockout, forecast_holt,
)

DEFAULT_HORIZON = 14
DEFAULT_LEAD_TIME_DAYS = 7   # 补货前置时间; 若 days_to_stockout < lead, throttle


def evaluate_with_forecast(rows: Iterable[Dict[str, Any]],
                           lead_time_days: int = DEFAULT_LEAD_TIME_DAYS,
                           horizon: int = DEFAULT_HORIZON,
                           ) -> Dict[str, Any]:
    throttle_skus: List[str] = []
    details: List[Dict[str, Any]] = []
    for row in rows:
        sku = row.get('sku')
        if not sku:
            continue
        stock = max(0, int(row.get('stock', 0)))
        history = row.get('history') or []
        fc = forecast_holt(history, horizon=horizon)
        days_out = days_to_stockout(stock, fc)
        avg = (sum(fc) / len(fc)) if fc else 0.0
        throttled = (
            avg > 0 and (days_out is not None and days_out <= lead_time_days)
        )
        if throttled:
            throttle_skus.append(sku)
        details.append({
            'sku': sku,
            'stock': stock,
            'avg_daily_forecast': round(avg, 4),
            'days_to_stockout': days_out,
            'lead_time_days': lead_time_days,
            'throttled': throttled,
        })
    return {
        'throttle_skus': throttle_skus,
        'lead_time_days': lead_time_days,
        'details': details,
        'total': len(details),
    }


def is_throttled_forecast(sku: str, report: Dict[str, Any]) -> bool:
    return sku in report.get('throttle_skus', [])
