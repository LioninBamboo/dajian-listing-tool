"""S108 — title/description 多臂老虎机优化器.

Thompson Sampling (Beta posterior) + 显著性近似检验.
不依赖 numpy: 用 random.betavariate.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, Iterable, List, Optional


def _alpha_beta(impressions: int, conversions: int):
    a = 1 + conversions
    b = 1 + max(0, impressions - conversions)
    return a, b


def posterior_mean(variant: Dict[str, Any]) -> float:
    a, b = _alpha_beta(int(variant.get('impressions', 0) or 0),
                        int(variant.get('conversions', 0) or 0))
    return a / (a + b)


def thompson_sample(variant: Dict[str, Any],
                    rng: Optional[random.Random] = None) -> float:
    a, b = _alpha_beta(int(variant.get('impressions', 0) or 0),
                        int(variant.get('conversions', 0) or 0))
    r = rng or random
    return r.betavariate(a, b)


def select_variant(variants: List[Dict[str, Any]],
                   *,
                   strategy: str = 'thompson',
                   epsilon: float = 0.10,
                   rng: Optional[random.Random] = None) -> Dict[str, Any]:
    if not variants:
        return {'id': None, 'reason': 'no_variants'}
    r = rng or random
    if strategy == 'epsilon_greedy':
        if r.random() < epsilon:
            chosen = r.choice(variants)
            reason = 'explore'
        else:
            chosen = max(variants, key=posterior_mean)
            reason = 'exploit'
        return {'id': chosen.get('id'),
                'posterior_mean': round(posterior_mean(chosen), 4),
                'reason': reason}
    # thompson
    sampled = [(thompson_sample(v, r), v) for v in variants]
    chosen = max(sampled, key=lambda t: t[0])[1]
    return {'id': chosen.get('id'),
            'posterior_mean': round(posterior_mean(chosen), 4),
            'reason': 'thompson'}


def _z_test(p1: float, n1: int, p2: float, n2: int) -> float:
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p = (p1 * n1 + p2 * n2) / (n1 + n2)
    se2 = p * (1 - p) * (1 / n1 + 1 / n2)
    if se2 <= 0:
        return 0.0
    return (p1 - p2) / math.sqrt(se2)


def is_significant(a: Dict[str, Any], b: Dict[str, Any],
                   z_threshold: float = 1.96) -> Dict[str, Any]:
    n1 = int(a.get('impressions', 0) or 0)
    n2 = int(b.get('impressions', 0) or 0)
    p1 = (a.get('conversions', 0) or 0) / n1 if n1 else 0
    p2 = (b.get('conversions', 0) or 0) / n2 if n2 else 0
    z = _z_test(p1, n1, p2, n2)
    return {
        'z_score': round(z, 4),
        'p1': round(p1, 4),
        'p2': round(p2, 4),
        'significant': abs(z) >= z_threshold,
        'winner': a.get('id') if z > 0 else (b.get('id') if z < 0 else None),
    }


def evaluate_variants(variants: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = []
    for v in variants:
        n = int(v.get('impressions', 0) or 0)
        c = int(v.get('conversions', 0) or 0)
        cvr = (c / n) if n else 0.0
        items.append({**v, 'cvr': round(cvr, 4),
                      'posterior_mean': round(posterior_mean(v), 4)})
    items.sort(key=lambda r: -r['posterior_mean'])
    winner = items[0] if items else None
    return {'items': items,
            'winner_id': winner['id'] if winner else None,
            'count': len(items)}
