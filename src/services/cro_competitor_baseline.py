"""S95 — 竞品价格基线监控.

输入: 同类目竞品 listings (注入 fetcher).
输出: median/p25/p75/min/max 价格 + 自家价格相对位置 + 折扣率排序.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional


def _percentile(sorted_prices: List[float], p: float) -> float:
    if not sorted_prices:
        return 0.0
    if len(sorted_prices) == 1:
        return sorted_prices[0]
    k = (len(sorted_prices) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_prices) - 1)
    if f == c:
        return sorted_prices[f]
    return sorted_prices[f] + (sorted_prices[c] - sorted_prices[f]) * (k - f)


def baseline_stats(prices: Iterable[float]) -> Dict[str, Any]:
    sp = sorted(float(p) for p in prices if p is not None and p > 0)
    if not sp:
        return {'count': 0, 'median': 0.0, 'p25': 0.0, 'p75': 0.0,
                'min': 0.0, 'max': 0.0, 'mean': 0.0}
    return {
        'count': len(sp),
        'median': round(_percentile(sp, 0.50), 2),
        'p25': round(_percentile(sp, 0.25), 2),
        'p75': round(_percentile(sp, 0.75), 2),
        'min': round(sp[0], 2),
        'max': round(sp[-1], 2),
        'mean': round(sum(sp) / len(sp), 2),
    }


def position_for_price(my_price: float,
                       stats: Dict[str, Any]) -> Dict[str, Any]:
    """返回自家定价处于竞品分布的位置."""
    if stats.get('count', 0) == 0 or my_price <= 0:
        return {'position': 'unknown', 'vs_median_pct': None,
                'recommendation': 'insufficient_data'}
    median = stats['median']
    diff_pct = (my_price - median) / median if median > 0 else 0
    if my_price < stats['p25']:
        position = 'cheap'
        rec = '低于市场 25 分位, 可考虑提价或强调价值'
    elif my_price < stats['median']:
        position = 'below_median'
        rec = '位于市场中下位, 价格有竞争力'
    elif my_price <= stats['p75']:
        position = 'above_median'
        rec = '位于市场中上位, 关注转化率'
    else:
        position = 'expensive'
        rec = '高于市场 75 分位, 需突出差异化或考虑降价'
    return {
        'position': position,
        'vs_median_pct': round(diff_pct, 4),
        'recommendation': rec,
    }


def fetch_competitor_baseline(category: str,
                              fetcher: Callable[[str], List[Dict[str, Any]]],
                              ) -> Dict[str, Any]:
    """fetcher(category) → [{price, discount_pct?}, ...]"""
    try:
        listings = list(fetcher(category) or [])
    except Exception:
        return {'category': category, 'error': 'fetcher_failed',
                'stats': baseline_stats([])}
    prices = [item.get('price') for item in listings if item]
    discounts = [float(item.get('discount_pct') or 0) for item in listings
                 if item and item.get('discount_pct')]
    stats = baseline_stats(prices)
    avg_discount = (sum(discounts) / len(discounts)) if discounts else 0.0
    return {
        'category': category,
        'stats': stats,
        'avg_discount_pct': round(avg_discount, 4),
        'sample_size': len(listings),
    }


def detect_underpriced(my_listings: Iterable[Dict[str, Any]],
                       baselines_by_category: Dict[str, Dict[str, Any]],
                       ) -> List[Dict[str, Any]]:
    """my_listings: [{sku, category, price}]; 返回低于 p25 的清单."""
    out: List[Dict[str, Any]] = []
    for it in my_listings:
        cat = it.get('category')
        baseline = baselines_by_category.get(cat) or {}
        stats = baseline.get('stats') or {}
        if stats.get('count', 0) == 0:
            continue
        if float(it.get('price', 0) or 0) < stats.get('p25', 0):
            out.append({
                'sku': it.get('sku'),
                'category': cat,
                'my_price': it.get('price'),
                'p25': stats.get('p25'),
                'median': stats.get('median'),
            })
    return out
