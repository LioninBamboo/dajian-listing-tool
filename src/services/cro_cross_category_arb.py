"""S118 — 跨类目价格套利发现.

同一/相似产品在不同 eBay 类目里售价差异 > 阈值 → 候选迁移到高价类目.
输入: catalog_rows=[{sku, title, category_id, current_price, sold_30d}],
      category_baselines={category_id: {median_price, sample}}.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

DEFAULT_MIN_LIFT = 0.20         # 至少 20% 溢价才推荐
DEFAULT_MIN_SAMPLE = 10         # 类目基线至少 10 个样本


def find_arbitrage_opportunities(
    catalog_rows: Iterable[Dict[str, Any]],
    category_baselines: Dict[Any, Dict[str, Any]],
    *,
    min_lift: float = DEFAULT_MIN_LIFT,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    candidate_categories: Iterable[Any] = None,
) -> List[Dict[str, Any]]:
    """对每条 SKU 在所有候选类目里找到 *中位价* 比当前 lift 更高的迁移机会."""
    candidate_categories = (
        list(candidate_categories) if candidate_categories
        else list(category_baselines.keys())
    )
    out: List[Dict[str, Any]] = []
    for r in catalog_rows:
        cur_cat = r.get('category_id')
        cur_price = float(r.get('current_price', 0) or 0)
        if cur_price <= 0:
            continue
        best = None
        for cat in candidate_categories:
            if cat == cur_cat:
                continue
            base = category_baselines.get(cat) or {}
            if int(base.get('sample', 0) or 0) < min_sample:
                continue
            median = float(base.get('median_price', 0) or 0)
            if median <= 0:
                continue
            lift = (median - cur_price) / cur_price
            if lift < min_lift:
                continue
            if best is None or lift > best['lift']:
                best = {'target_category': cat,
                        'target_median_price': round(median, 2),
                        'lift': round(lift, 4)}
        if best:
            out.append({
                'sku': r.get('sku'),
                'title': r.get('title'),
                'current_category': cur_cat,
                'current_price': cur_price,
                'sold_30d': int(r.get('sold_30d', 0) or 0),
                **best,
                'recommended_price': round(best['target_median_price'] * 0.95, 2),
            })
    out.sort(key=lambda x: -x['lift'])
    return out


def summarise(opportunities: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not opportunities:
        return {'count': 0, 'total_potential_lift_usd': 0.0}
    total_lift = sum(
        (o['recommended_price'] - o['current_price']) * o['sold_30d']
        for o in opportunities
    )
    return {
        'count': len(opportunities),
        'top_3': opportunities[:3],
        'total_potential_lift_usd': round(total_lift, 2),
    }
