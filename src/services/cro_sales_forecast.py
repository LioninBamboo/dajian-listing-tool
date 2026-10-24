"""S67 — 销量预测 (Simple Exponential Smoothing + 线性趋势).

不依赖 statsmodels/sklearn; 输入按天聚合的销量列表, 输出 H 天预测.
给 cro_inventory_throttle 提供更精准的 days_runway 替代固定均值.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

DEFAULT_ALPHA = 0.3   # level 平滑
DEFAULT_BETA = 0.1    # trend 平滑
MIN_HISTORY = 7


def _validate(history: Sequence[float]) -> List[float]:
    return [max(0.0, float(x)) for x in history]


def forecast_holt(history: Sequence[float],
                  horizon: int = 7,
                  alpha: float = DEFAULT_ALPHA,
                  beta: float = DEFAULT_BETA,
                  ) -> List[float]:
    """Holt 双参指数平滑 (level+trend), 无季节项."""
    series = _validate(history)
    if len(series) < MIN_HISTORY:
        # fallback: 复用最后一个均值
        avg = (sum(series) / len(series)) if series else 0.0
        return [avg] * horizon
    level = series[0]
    trend = series[1] - series[0]
    for x in series[1:]:
        prev_level = level
        level = alpha * x + (1 - alpha) * (level + trend)
        trend = beta * (level - prev_level) + (1 - beta) * trend
    return [max(0.0, level + (h + 1) * trend) for h in range(horizon)]


def forecast_simple_avg(history: Sequence[float],
                        horizon: int = 7) -> List[float]:
    series = _validate(history)
    avg = (sum(series) / len(series)) if series else 0.0
    return [avg] * horizon


def days_to_stockout(stock_units: float,
                     daily_forecast: Sequence[float]) -> Optional[int]:
    if stock_units <= 0:
        return 0
    cum = 0.0
    for i, demand in enumerate(daily_forecast):
        cum += demand
        if cum >= stock_units:
            return i + 1
    return None  # forecast 期内不会断货


def forecast_summary(history: Sequence[float],
                     stock_units: float,
                     horizon: int = 14,
                     ) -> Dict[str, object]:
    fc = forecast_holt(history, horizon=horizon)
    avg_demand = sum(fc) / len(fc) if fc else 0.0
    days_out = days_to_stockout(stock_units, fc)
    return {
        'forecast': [round(x, 4) for x in fc],
        'avg_daily_demand': round(avg_demand, 4),
        'days_to_stockout': days_out,
        'horizon': horizon,
        'method': ('holt' if len(history) >= MIN_HISTORY
                   else 'fallback_avg'),
    }
