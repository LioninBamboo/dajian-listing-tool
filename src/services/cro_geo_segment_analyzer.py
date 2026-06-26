"""S104 — 地域销售分段分析.

输入: orders=[{state, zip, qty, revenue}].
输出: 按 state / zip3 (前3位) 聚合 + Top N + 集中度 (HHI 指数).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List


def _zip3(z: str) -> str:
    if not z:
        return 'UNKNOWN'
    z = str(z).strip()
    return z[:3] if len(z) >= 3 else z


def aggregate_by_state(orders: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bucket: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {'qty': 0, 'revenue': 0.0, 'orders': 0})
    for o in orders:
        state = (o.get('state') or 'UNKNOWN').upper()
        b = bucket[state]
        b['qty'] += int(o.get('qty', 0) or 0)
        b['revenue'] += float(o.get('revenue', 0) or 0)
        b['orders'] += 1
    out = []
    for state, v in bucket.items():
        out.append({
            'state': state,
            'qty': int(v['qty']),
            'revenue': round(v['revenue'], 2),
            'orders': int(v['orders']),
            'aov': round(v['revenue'] / v['orders'], 2) if v['orders'] else 0.0,
        })
    return sorted(out, key=lambda r: -r['revenue'])


def aggregate_by_zip3(orders: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bucket: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {'qty': 0, 'revenue': 0.0, 'orders': 0})
    for o in orders:
        z3 = _zip3(o.get('zip', ''))
        b = bucket[z3]
        b['qty'] += int(o.get('qty', 0) or 0)
        b['revenue'] += float(o.get('revenue', 0) or 0)
        b['orders'] += 1
    out = []
    for z3, v in bucket.items():
        out.append({'zip3': z3,
                    'qty': int(v['qty']),
                    'revenue': round(v['revenue'], 2),
                    'orders': int(v['orders'])})
    return sorted(out, key=lambda r: -r['revenue'])


def hhi(by_state: List[Dict[str, Any]]) -> float:
    """Herfindahl-Hirschman Index 0-10000; 越高越集中."""
    total = sum(s['revenue'] for s in by_state)
    if total <= 0:
        return 0.0
    shares = [(s['revenue'] / total) * 100 for s in by_state]
    return round(sum(s ** 2 for s in shares), 2)


def concentration_label(hhi_value: float) -> str:
    if hhi_value >= 2500:
        return 'highly_concentrated'
    if hhi_value >= 1500:
        return 'moderately_concentrated'
    return 'diversified'


def analyze_geo(orders: Iterable[Dict[str, Any]],
                top_n: int = 5) -> Dict[str, Any]:
    by_state = aggregate_by_state(orders)
    by_zip = aggregate_by_zip3(orders)
    h = hhi(by_state)
    return {
        'by_state': by_state,
        'by_zip3': by_zip,
        'top_states': by_state[:top_n],
        'top_zip3': by_zip[:top_n],
        'state_count': len(by_state),
        'hhi': h,
        'concentration': concentration_label(h),
        'total_orders': sum(s['orders'] for s in by_state),
        'total_revenue': round(sum(s['revenue'] for s in by_state), 2),
    }
