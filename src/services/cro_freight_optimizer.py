"""S99 — 运费档位 vs CVR 弹性.

输入: 历史 [{freight, cvr, sample_size}] 行.
输出: 各档位 CVR + 推荐档位 (考虑样本量门槛, 避免噪音).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

DEFAULT_MIN_SAMPLE = 50


def aggregate_by_freight(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bucket: Dict[float, Dict[str, float]] = {}
    for r in rows:
        try:
            f = round(float(r.get('freight', 0) or 0), 2)
        except (TypeError, ValueError):
            continue
        cvr = float(r.get('cvr', 0) or 0)
        n = int(r.get('sample_size', 0) or 0)
        if n <= 0:
            continue
        b = bucket.setdefault(f, {'sum_cvr_n': 0.0, 'n': 0})
        b['sum_cvr_n'] += cvr * n
        b['n'] += n
    out = []
    for f, b in sorted(bucket.items()):
        weighted_cvr = b['sum_cvr_n'] / b['n'] if b['n'] else 0.0
        out.append({
            'freight': f,
            'avg_cvr': round(weighted_cvr, 6),
            'sample_size': int(b['n']),
        })
    return out


def recommend_freight_tier(rows: Iterable[Dict[str, Any]],
                           *,
                           min_sample: int = DEFAULT_MIN_SAMPLE,
                           ) -> Dict[str, Any]:
    buckets = aggregate_by_freight(rows)
    eligible = [b for b in buckets if b['sample_size'] >= min_sample]
    if not eligible:
        return {'recommended_freight': None,
                'reason': 'insufficient_samples',
                'buckets': buckets}
    best = max(eligible, key=lambda b: b['avg_cvr'])
    return {
        'recommended_freight': best['freight'],
        'recommended_cvr': best['avg_cvr'],
        'sample_size': best['sample_size'],
        'buckets': buckets,
        'eligible_count': len(eligible),
    }


def freight_elasticity(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """估算 dCVR/dFreight (简单线性差分, 非回归)."""
    buckets = aggregate_by_freight(rows)
    if len(buckets) < 2:
        return {'elasticity': None, 'buckets': buckets}
    pairs = list(zip(buckets, buckets[1:]))
    slopes = []
    for a, b in pairs:
        df = b['freight'] - a['freight']
        if df == 0:
            continue
        slopes.append((b['avg_cvr'] - a['avg_cvr']) / df)
    if not slopes:
        return {'elasticity': None, 'buckets': buckets}
    avg_slope = sum(slopes) / len(slopes)
    return {
        'elasticity': round(avg_slope, 6),
        'direction': 'negative' if avg_slope < 0 else 'positive',
        'buckets': buckets,
    }
