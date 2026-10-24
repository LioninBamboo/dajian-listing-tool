"""S103 — 变异系数动态安全库存.

公式: SS = z * sigma_demand * sqrt(lead_time)
- mean_daily / sigma_daily 来自最近 N 天销量
- 变异系数 CV=sigma/mean 决定服务水平 z 推荐值
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List

# 服务水平 → z 值 (近似)
SERVICE_LEVEL_Z = {
    0.85: 1.04,
    0.90: 1.28,
    0.95: 1.65,
    0.97: 1.88,
    0.99: 2.33,
}


def _mean_std(xs: List[float]):
    if not xs:
        return 0.0, 0.0
    n = len(xs)
    m = sum(xs) / n
    if n == 1:
        return m, 0.0
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var)


def cv(xs: List[float]) -> float:
    mean, std = _mean_std(xs)
    if mean <= 0:
        return 0.0
    return std / mean


def recommend_service_level(cv_value: float) -> float:
    """高变异 → 高服务水平."""
    if cv_value >= 1.0:
        return 0.99
    if cv_value >= 0.6:
        return 0.97
    if cv_value >= 0.3:
        return 0.95
    return 0.90


def compute_safety_stock(history: Iterable[float],
                         lead_time_days: int,
                         *,
                         service_level: float = None) -> Dict[str, Any]:
    xs = [float(x or 0) for x in history]
    if not xs or lead_time_days <= 0:
        return {'safety_stock': 0, 'reorder_point': 0,
                'mean_daily': 0.0, 'std_daily': 0.0,
                'cv': 0.0, 'z': 0.0,
                'reason': 'insufficient_data'}
    mean, std = _mean_std(xs)
    cv_v = cv(xs)
    sl = service_level or recommend_service_level(cv_v)
    z = SERVICE_LEVEL_Z.get(round(sl, 2), 1.65)
    ss = z * std * math.sqrt(lead_time_days)
    reorder_point = mean * lead_time_days + ss
    return {
        'safety_stock': max(0, int(round(ss))),
        'reorder_point': max(0, int(round(reorder_point))),
        'mean_daily': round(mean, 4),
        'std_daily': round(std, 4),
        'cv': round(cv_v, 4),
        'service_level': sl,
        'z': z,
        'lead_time_days': lead_time_days,
    }


def batch_compute(rows: Iterable[Dict[str, Any]],
                  *,
                  default_lead_time: int = 14) -> Dict[str, Any]:
    """rows: [{sku, history, lead_time_days, on_hand}]"""
    items: List[Dict[str, Any]] = []
    needs_reorder = 0
    for r in rows:
        lt = int(r.get('lead_time_days', default_lead_time) or default_lead_time)
        out = compute_safety_stock(r.get('history') or [], lt)
        out['sku'] = r.get('sku')
        out['on_hand'] = int(r.get('on_hand', 0) or 0)
        out['needs_reorder'] = out['on_hand'] <= out['reorder_point']
        if out['needs_reorder']:
            needs_reorder += 1
        items.append(out)
    return {'items': items, 'count': len(items),
            'needs_reorder_count': needs_reorder}
