"""S91 — CUSUM 变点检测.

监测时间序列中均值/方差的突变 (例如 CTR 突降, 退货率突升).
纯 Python 实现, 无 numpy/scipy. CUSUM:
  S_t = max(0, S_{t-1} + (x_t - target - k))
当 S_t > threshold 视为正向变点 (上升), 负向同理.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

DEFAULT_K_RATIO = 0.5  # 半个标准差容许带
DEFAULT_H_RATIO = 5.0  # 5 倍标准差触发


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return var ** 0.5


def detect_change_points(series: Iterable[float],
                         *,
                         baseline_window: Optional[int] = None,
                         k_ratio: float = DEFAULT_K_RATIO,
                         h_ratio: float = DEFAULT_H_RATIO,
                         ) -> Dict[str, Any]:
    xs = [float(x) for x in series]
    n = len(xs)
    if n < 4:
        return {'change_points': [], 'reason': 'insufficient_data',
                'n': n}
    baseline_window = baseline_window or max(3, n // 4)
    base = xs[:baseline_window]
    target = _mean(base)
    sigma = _stdev(base) or 1e-9
    k = k_ratio * sigma
    h = h_ratio * sigma

    pos: float = 0.0
    neg: float = 0.0
    points: List[Dict[str, Any]] = []
    for i in range(baseline_window, n):
        x = xs[i]
        pos = max(0.0, pos + (x - target - k))
        neg = min(0.0, neg + (x - target + k))
        if pos > h:
            points.append({'index': i, 'value': x, 'direction': 'up',
                           'cusum': round(pos, 6)})
            pos = 0.0
        elif -neg > h:
            points.append({'index': i, 'value': x, 'direction': 'down',
                           'cusum': round(-neg, 6)})
            neg = 0.0
    return {
        'change_points': points,
        'baseline_window': baseline_window,
        'baseline_mean': round(target, 6),
        'baseline_stdev': round(sigma, 6),
        'k': round(k, 6),
        'h': round(h, 6),
        'n': n,
    }


def first_change_point(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pts = result.get('change_points') or []
    return pts[0] if pts else None


def summarise_change(result: Dict[str, Any]) -> str:
    pts = result.get('change_points') or []
    if not pts:
        return '序列稳定, 无显著变点'
    first = pts[0]
    arrow = '↑' if first['direction'] == 'up' else '↓'
    return (f'共 {len(pts)} 个变点, 首个在 index={first["index"]} '
            f'方向 {arrow} (cusum={first["cusum"]})')
