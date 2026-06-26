"""S75 — 季节性周期检测 (autocorrelation).

不依赖 numpy/scipy. 输入按天聚合的销量, 检测 7/14/30 天周期 strength.
strength = autocorr(lag), 范围 [-1, 1]; >= 0.30 视为 "明显周期".
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_LAGS = (7, 14, 30, 90)
SIGNIFICANT = 0.30


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _autocorr(series: Sequence[float], lag: int) -> Optional[float]:
    n = len(series)
    if lag <= 0 or lag >= n:
        return None
    mu = _mean(series)
    num = 0.0
    den = 0.0
    for i in range(n - lag):
        num += (series[i] - mu) * (series[i + lag] - mu)
    for x in series:
        den += (x - mu) ** 2
    if den <= 1e-12:
        return None
    return num / den


def detect_seasonality(history: Sequence[float],
                       lags: Sequence[int] = DEFAULT_LAGS,
                       threshold: float = SIGNIFICANT,
                       ) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    best_lag = None
    best_corr = -2.0
    for lag in lags:
        corr = _autocorr(history, lag)
        rows.append({
            'lag': lag,
            'autocorr': round(corr, 4) if corr is not None else None,
            'significant': bool(corr is not None and corr >= threshold),
        })
        if corr is not None and corr > best_corr:
            best_corr = corr
            best_lag = lag
    significant = [r for r in rows if r['significant']]
    label = (f'period_{best_lag}d'
             if (significant and best_lag is not None) else 'no_clear_period')
    return {
        'rows': rows,
        'best_lag': best_lag if significant else None,
        'best_autocorr': (round(best_corr, 4)
                          if significant else None),
        'period_label': label,
        'sample_size': len(history),
    }


def fft_dominant_period(history: Sequence[float],
                        max_period: Optional[int] = None,
                        ) -> Optional[int]:
    """简化 DFT: 仅返回幅度最高的频率对应的周期; 无 numpy."""
    n = len(history)
    if n < 4:
        return None
    mu = _mean(history)
    centered = [x - mu for x in history]
    cap = max_period or n // 2
    best_period = None
    best_amp = 0.0
    for k in range(1, n // 2 + 1):
        if k == 0:
            continue
        re = 0.0
        im = 0.0
        for t, x in enumerate(centered):
            angle = -2 * math.pi * k * t / n
            re += x * math.cos(angle)
            im += x * math.sin(angle)
        amp = math.hypot(re, im)
        period = n / k
        if period > cap:
            continue
        if amp > best_amp:
            best_amp = amp
            best_period = int(round(period))
    return best_period
