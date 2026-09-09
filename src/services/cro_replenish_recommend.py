"""S86 — 补货推荐.

输入: SKU 销售历史 + 当前库存 + 供应商 lead_time + 安全库存系数.
输出: 推荐补货量 = forecast_during_leadtime + safety_stock - on_hand - on_order.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from src.services.cro_sales_forecast import (
    forecast_holt, MIN_HISTORY,
)

DEFAULT_LEAD_TIME_DAYS = 7
DEFAULT_SAFETY_DAYS = 3


def _avg_daily(history: List[float]) -> float:
    if not history:
        return 0.0
    return sum(history) / len(history)


def recommend_replenish(sku: str,
                        history: List[float],
                        on_hand: int,
                        *,
                        on_order: int = 0,
                        lead_time_days: int = DEFAULT_LEAD_TIME_DAYS,
                        safety_days: int = DEFAULT_SAFETY_DAYS,
                        ) -> Dict[str, Any]:
    horizon = lead_time_days + safety_days
    if len(history) >= MIN_HISTORY:
        forecast = forecast_holt(history, horizon=horizon)
    else:
        avg = _avg_daily(history)
        forecast = [avg] * horizon
    needed_units = sum(forecast)
    available = (on_hand or 0) + (on_order or 0)
    qty = max(0, int(round(needed_units - available)))
    avg_daily = _avg_daily(history)
    days_runway = (on_hand / avg_daily) if avg_daily > 0 else float('inf')
    return {
        'sku': sku,
        'recommend_qty': qty,
        'forecast_units_horizon': round(needed_units, 2),
        'horizon_days': horizon,
        'lead_time_days': lead_time_days,
        'safety_days': safety_days,
        'on_hand': on_hand,
        'on_order': on_order,
        'avg_daily': round(avg_daily, 4),
        'days_runway': (round(days_runway, 2)
                        if days_runway != float('inf') else None),
        'urgency': _urgency(days_runway, lead_time_days),
    }


def _urgency(days_runway: float, lead_time_days: int) -> str:
    if days_runway == float('inf'):
        return 'none'
    if days_runway <= 0:
        return 'critical'
    if days_runway <= lead_time_days:
        return 'high'
    if days_runway <= lead_time_days * 2:
        return 'medium'
    return 'low'


def batch_recommend(rows: Iterable[Dict[str, Any]],
                    *,
                    lead_time_days: int = DEFAULT_LEAD_TIME_DAYS,
                    safety_days: int = DEFAULT_SAFETY_DAYS,
                    ) -> Dict[str, Any]:
    """rows: [{sku, history, on_hand, on_order?}, ...]"""
    items: List[Dict[str, Any]] = []
    for r in rows:
        items.append(recommend_replenish(
            r.get('sku', ''),
            list(r.get('history') or []),
            int(r.get('on_hand', 0) or 0),
            on_order=int(r.get('on_order', 0) or 0),
            lead_time_days=lead_time_days,
            safety_days=safety_days,
        ))
    needs = [it for it in items if it['recommend_qty'] > 0]
    return {
        'items': items,
        'needs_replenish_count': len(needs),
        'critical_count': sum(1 for it in items
                              if it['urgency'] == 'critical'),
        'total_units_to_order': sum(it['recommend_qty'] for it in items),
    }
