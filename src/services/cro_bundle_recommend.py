"""S105 — 商品关联推荐 (Apriori 简化版).

输入: transactions=[[sku, sku, ...]] (购物车级)
输出: 频繁项对 (support, confidence, lift) → 推荐 bundle.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from typing import Any, Dict, Iterable, List, Tuple


def item_support(transactions: List[List[str]]) -> Dict[str, float]:
    n = len(transactions)
    if n == 0:
        return {}
    counter: Counter = Counter()
    for tx in transactions:
        for sku in set(tx):
            counter[sku] += 1
    return {sku: c / n for sku, c in counter.items()}


def pair_counts(transactions: List[List[str]],
                min_item_support: float = 0.0) -> Dict[Tuple[str, str], int]:
    """先按 min_item_support 剪枝, 再统计 pair."""
    n = len(transactions)
    if n == 0:
        return {}
    item_sup = item_support(transactions)
    eligible = {sku for sku, s in item_sup.items() if s >= min_item_support}
    pair_count: Dict[Tuple[str, str], int] = defaultdict(int)
    for tx in transactions:
        items = sorted(set(s for s in tx if s in eligible))
        for a, b in combinations(items, 2):
            pair_count[(a, b)] += 1
    return dict(pair_count)


def find_associations(transactions: List[List[str]],
                      *,
                      min_support: float = 0.05,
                      min_confidence: float = 0.30,
                      min_lift: float = 1.10,
                      ) -> List[Dict[str, Any]]:
    n = len(transactions)
    if n == 0:
        return []
    item_sup = item_support(transactions)
    pairs = pair_counts(transactions, min_item_support=min_support)
    out: List[Dict[str, Any]] = []
    for (a, b), c in pairs.items():
        sup_ab = c / n
        if sup_ab < min_support:
            continue
        sup_a = item_sup.get(a, 0)
        sup_b = item_sup.get(b, 0)
        if sup_a <= 0 or sup_b <= 0:
            continue
        # A→B
        conf_a = sup_ab / sup_a
        lift_ab = sup_ab / (sup_a * sup_b)
        if conf_a >= min_confidence and lift_ab >= min_lift:
            out.append({
                'antecedent': a, 'consequent': b,
                'support': round(sup_ab, 4),
                'confidence': round(conf_a, 4),
                'lift': round(lift_ab, 4),
            })
        # B→A
        conf_b = sup_ab / sup_b
        if conf_b >= min_confidence and lift_ab >= min_lift:
            out.append({
                'antecedent': b, 'consequent': a,
                'support': round(sup_ab, 4),
                'confidence': round(conf_b, 4),
                'lift': round(lift_ab, 4),
            })
    return sorted(out, key=lambda r: -r['lift'])


def recommend_for_sku(sku: str,
                      associations: List[Dict[str, Any]],
                      top_n: int = 3) -> List[Dict[str, Any]]:
    matches = [a for a in associations if a['antecedent'] == sku]
    return matches[:top_n]
