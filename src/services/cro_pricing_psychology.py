"""S113 — 心理定价格点对齐.

输入: target_price, min_price, max_price (可选)
输出: 推荐 charm price (.99/.95/.49) 同时在合规区间内.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional

# 优先级: .99 > .95 > .49 > .89 > .79
CHARM_ENDINGS = (0.99, 0.95, 0.49, 0.89, 0.79)


def _quantize(price: float, ending: float) -> float:
    """把价格调整到 floor + ending."""
    if price <= 0:
        return 0.0
    base = math.floor(price)
    candidate = base + ending
    # 如果候选>原价>0.5 或者候选<原价 都接受, 取最近
    if candidate > price + 0.50:
        candidate -= 1.0
    return round(max(0.01, candidate), 2)


def candidate_charm_prices(target: float) -> List[float]:
    """对每个 charm ending 生成候选."""
    return list(dict.fromkeys(_quantize(target, e) for e in CHARM_ENDINGS))


def recommend_charm_price(target: float,
                           *,
                           min_price: Optional[float] = None,
                           max_price: Optional[float] = None,
                           max_deviation_pct: float = 0.05,
                           ) -> Dict[str, Any]:
    """从候选里选择 *绝对偏差最小且合规* 的 charm 价."""
    if target <= 0:
        return {'recommended': None, 'reason': 'invalid_target'}
    candidates = []
    for ending in CHARM_ENDINGS:
        c = _quantize(target, ending)
        if c <= 0:
            continue
        if min_price is not None and c < min_price:
            continue
        if max_price is not None and c > max_price:
            continue
        deviation = abs(c - target) / target
        if deviation > max_deviation_pct:
            continue
        candidates.append({'price': c, 'ending': ending,
                            'deviation': round(deviation, 4)})
    if not candidates:
        return {'recommended': None, 'reason': 'no_candidate_in_range',
                'target': target}
    # 取偏差最小, 同偏差按 ending 优先级
    pri = {e: i for i, e in enumerate(CHARM_ENDINGS)}
    candidates.sort(key=lambda c: (c['deviation'], pri.get(c['ending'], 99)))
    best = candidates[0]
    return {
        'recommended': best['price'],
        'ending': best['ending'],
        'deviation_pct': best['deviation'],
        'target': target,
        'all_candidates': candidates,
    }


def is_charm_price(price: float, tol: float = 0.005) -> bool:
    if price <= 0:
        return False
    frac = price - math.floor(price)
    return any(abs(frac - e) < tol for e in CHARM_ENDINGS)


def batch_align(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """rows: [{sku, target_price, min_price, max_price}]"""
    items = []
    aligned = 0
    for r in rows:
        out = recommend_charm_price(
            float(r.get('target_price', 0) or 0),
            min_price=r.get('min_price'),
            max_price=r.get('max_price'),
        )
        out['sku'] = r.get('sku')
        items.append(out)
        if out.get('recommended'):
            aligned += 1
    return {'items': items, 'count': len(items), 'aligned_count': aligned}
