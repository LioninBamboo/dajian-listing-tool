"""S106 — 产品生命周期阶段分类.

新品 (new) / 成长 (growth) / 成熟 (mature) / 衰退 (decline) / 滞销 (stagnant).
依据: 上架天数 + 近期销量趋势 + 销量绝对值.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _slope(xs: List[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(xs) / n
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(xs))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0.0
    return num / den


def classify_lifecycle(features: Dict[str, Any]) -> Dict[str, Any]:
    """features keys:
       age_days (int), recent_sales (list[float], 最近 N 天每日销量),
       total_sales (int), avg_recent_daily (float, optional)."""
    age = int(features.get('age_days', 0) or 0)
    sales = [float(x or 0) for x in (features.get('recent_sales') or [])]
    total = int(features.get('total_sales', 0) or 0)
    avg_recent = (sum(sales) / len(sales)) if sales else 0.0
    slope = _slope(sales)

    # 1. 新品: 上架 ≤14 天
    if age <= 14:
        stage = 'new'
        reason = f'上架仅 {age} 天, 处于新品观察期'
    # 2. 滞销: 上架 ≥30 天但近期日均 <0.1
    elif avg_recent < 0.1 and age >= 30:
        stage = 'stagnant'
        reason = f'上架 {age} 天但近期日均仅 {avg_recent:.2f}'
    # 3. 成长: 销量明显上升 (slope >0.05) 且 age<90
    elif slope > 0.05 and age < 90:
        stage = 'growth'
        reason = f'销量上升 (slope={slope:.3f}), 处于成长期'
    # 4. 衰退: 销量下降 (slope <-0.05)
    elif slope < -0.05:
        stage = 'decline'
        reason = f'销量下降 (slope={slope:.3f}), 处于衰退期'
    # 5. 成熟: 其余
    else:
        stage = 'mature'
        reason = f'销量平稳 (slope={slope:.3f}), 处于成熟期'

    return {
        'stage': stage,
        'reason': reason,
        'age_days': age,
        'avg_recent_daily': round(avg_recent, 4),
        'slope': round(slope, 4),
        'total_sales': total,
        'recommendation': _recommendation_for(stage),
    }


def _recommendation_for(stage: str) -> str:
    return {
        'new': '观察前 14 天表现, 暂不调价/下架',
        'growth': '加大推广 + 适度提价测试',
        'mature': '保持竞争力价格, 关注转化率',
        'decline': '减少广告 / 重新定位 / 准备下一代替换',
        'stagnant': '优先下架或换主图+改标题救活',
    }.get(stage, '继续观察')


def batch_classify(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    items = [dict(classify_lifecycle(r), sku=r.get('sku')) for r in rows]
    counts: Dict[str, int] = {}
    for it in items:
        counts[it['stage']] = counts.get(it['stage'], 0) + 1
    return {'items': items, 'count': len(items), 'by_stage': counts}
