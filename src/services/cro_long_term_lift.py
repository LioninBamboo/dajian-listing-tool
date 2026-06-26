"""S102 — 长期 lift 衰减监测.

输入: 周/天序列 [{period_index, lift_pct}].
输出: 拟合衰减半周期 (lift 折半所需周数) + 当前残余 lift + 是否衰减完毕.
模型: 简化指数衰减 lift(t) = L0 * exp(-k*t); k 从 log-linear 回归.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def _log_linear_fit(ts: List[float],
                    lifts: List[float]) -> Optional[Dict[str, float]]:
    """log(lift) = log(L0) - k*t → 线性回归 (要求 lift>0)."""
    pts = [(t, math.log(l)) for t, l in zip(ts, lifts) if l and l > 0]
    n = len(pts)
    if n < 2:
        return None
    mean_t = sum(p[0] for p in pts) / n
    mean_y = sum(p[1] for p in pts) / n
    num = sum((t - mean_t) * (y - mean_y) for t, y in pts)
    den = sum((t - mean_t) ** 2 for t, y in pts)
    if den == 0:
        return None
    slope = num / den
    intercept = mean_y - slope * mean_t
    return {'slope': slope, 'intercept': intercept, 'L0': math.exp(intercept)}


def fit_decay(series: List[Dict[str, Any]]) -> Dict[str, Any]:
    """series: [{period_index:int, lift_pct:float}]"""
    if not series:
        return {'status': 'empty', 'half_life': None, 'k': None}
    ordered = sorted(series, key=lambda r: r.get('period_index', 0))
    ts = [r['period_index'] for r in ordered]
    lifts = [float(r.get('lift_pct', 0) or 0) for r in ordered]
    fit = _log_linear_fit(ts, lifts)
    if not fit:
        return {'status': 'cannot_fit', 'half_life': None,
                'k': None, 'series_len': len(ordered)}
    k = -fit['slope']  # 衰减率
    if k <= 0:
        return {'status': 'no_decay', 'half_life': None, 'k': round(k, 6),
                'L0': round(fit['L0'], 6), 'note': 'lift 在上升或持平'}
    half_life = math.log(2) / k
    last_t = ts[-1]
    residual = fit['L0'] * math.exp(-k * last_t)
    return {
        'status': 'fit_ok',
        'k': round(k, 6),
        'L0': round(fit['L0'], 6),
        'half_life_periods': round(half_life, 4),
        'residual_lift_at_last_period': round(residual, 6),
        'series_len': len(ordered),
    }


def is_decay_complete(fit_result: Dict[str, Any],
                      *,
                      threshold: float = 0.05) -> bool:
    """残余 lift < threshold (默认 5%) 视为衰减完毕."""
    res = fit_result.get('residual_lift_at_last_period')
    return res is not None and res < threshold


def project_future_lift(fit_result: Dict[str, Any],
                        future_periods: int) -> Optional[float]:
    """预测 future_periods 期后的 lift."""
    if fit_result.get('status') != 'fit_ok':
        return None
    k = fit_result['k']
    L0 = fit_result['L0']
    series_len = fit_result.get('series_len', 1)
    t = series_len + future_periods
    return round(L0 * math.exp(-k * t), 6)


def batch_fit(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows: [{sku, series:[{period_index,lift_pct}]}]"""
    items = []
    decayed = 0
    for r in rows:
        fit = fit_decay(r.get('series') or [])
        item = {'sku': r.get('sku'), **fit}
        item['decay_complete'] = is_decay_complete(fit)
        if item['decay_complete']:
            decayed += 1
        items.append(item)
    return {'items': items, 'count': len(items), 'decayed_count': decayed}
