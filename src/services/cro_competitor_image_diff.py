"""S107 — 主图差异度评估.

接收 *预提取* 颜色直方图 (dict 或 list), 不依赖 PIL.
支持 chi_square / euclidean / cosine / intersection 距离.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple, Union

Hist = Union[Dict[Any, float], Sequence[float]]


def _to_list(h: Hist) -> List[float]:
    if isinstance(h, dict):
        return [float(v) for v in h.values()]
    return [float(v) for v in h]


def _normalize(h: List[float]) -> List[float]:
    s = sum(h)
    if s <= 0:
        return [0.0 for _ in h]
    return [v / s for v in h]


def _align(h1: List[float], h2: List[float]) -> Tuple[List[float], List[float]]:
    n = max(len(h1), len(h2))
    return h1 + [0.0] * (n - len(h1)), h2 + [0.0] * (n - len(h2))


def histogram_distance(h1: Hist, h2: Hist,
                       method: str = 'chi_square') -> float:
    a, b = _align(_normalize(_to_list(h1)), _normalize(_to_list(h2)))
    if method == 'euclidean':
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
    if method == 'cosine':
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 1.0
        return 1.0 - (dot / (na * nb))
    if method == 'intersection':
        return 1.0 - sum(min(x, y) for x, y in zip(a, b))
    # chi_square (默认): sum((a-b)^2 / (a+b))
    s = 0.0
    for x, y in zip(a, b):
        d = x + y
        if d > 0:
            s += ((x - y) ** 2) / d
    return s


def differentiation_score(target: Hist,
                          competitors: List[Hist],
                          method: str = 'chi_square') -> Dict[str, Any]:
    """0-1: 越接近 1 → 我的图越独特; 越接近 0 → 与竞品相似."""
    if not competitors:
        return {'score': 1.0, 'avg_distance': 0.0, 'min_distance': 0.0,
                'competitor_count': 0}
    dists = [histogram_distance(target, c, method=method) for c in competitors]
    avg = sum(dists) / len(dists)
    mn = min(dists)
    # chi_square 上界 ~2; 其余 ~1; 统一截断到 [0,1]
    raw = avg if method != 'chi_square' else min(avg, 1.0)
    return {
        'score': round(max(0.0, min(1.0, raw)), 4),
        'avg_distance': round(avg, 4),
        'min_distance': round(mn, 4),
        'competitor_count': len(competitors),
        'method': method,
    }


def find_most_similar(target: Hist,
                      candidates: List[Hist],
                      method: str = 'chi_square'
                      ) -> Dict[str, Any]:
    if not candidates:
        return {'index': -1, 'distance': None}
    dists = [histogram_distance(target, c, method=method) for c in candidates]
    idx = min(range(len(dists)), key=lambda i: dists[i])
    return {'index': idx, 'distance': round(dists[idx], 4)}
