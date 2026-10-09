"""S111 — 高退货 SKU 预防性下架候选生成.

依据: 退货率 + 销量基数 + 主因聚类 → 给出 'pause'|'review'|'keep' 决策.
不直接下架, 只生成候选清单供 magic-link delist 走 S25.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List

# 阈值
HIGH_RETURN_RATE = 0.20      # >=20% 退货率
MIN_SOLD_FOR_PAUSE = 10      # 销量太少不下架
REVIEW_RETURN_RATE = 0.10    # 10-20% 进 review
QUALITY_REASONS = {'QUALITY', 'DAMAGED', 'DEFECTIVE', 'NOT_AS_DESCRIBED'}
SIZE_REASONS = {'SIZE', 'WRONG_SIZE', 'TOO_SMALL', 'TOO_LARGE'}


def _return_rate(sold: int, returned: int) -> float:
    if sold <= 0:
        return 0.0
    return returned / sold


def _classify_dominant_reason(reason_counts: Dict[str, int]) -> str:
    if not reason_counts:
        return 'unknown'
    quality = sum(c for r, c in reason_counts.items()
                   if r.upper() in QUALITY_REASONS)
    size = sum(c for r, c in reason_counts.items()
                if r.upper() in SIZE_REASONS)
    if quality >= size and quality > 0:
        return 'quality'
    if size > 0:
        return 'size'
    # fallback: top key
    return max(reason_counts.items(), key=lambda x: x[1])[0].lower()


def evaluate_sku(row: Dict[str, Any]) -> Dict[str, Any]:
    """row: {sku, sold_30d, returned_30d, reason_counts:{REASON:n}}"""
    sku = row.get('sku')
    sold = int(row.get('sold_30d', 0) or 0)
    returned = int(row.get('returned_30d', 0) or 0)
    rate = _return_rate(sold, returned)
    reason_counts = row.get('reason_counts') or {}
    dominant = _classify_dominant_reason(reason_counts)

    if sold < MIN_SOLD_FOR_PAUSE:
        decision = 'keep'
        reason = f'销量仅 {sold} 件, 样本不足'
    elif rate >= HIGH_RETURN_RATE:
        decision = 'pause'
        reason = f'退货率 {rate:.1%} 超阈, 主因={dominant}'
    elif rate >= REVIEW_RETURN_RATE:
        decision = 'review'
        reason = f'退货率 {rate:.1%} 偏高, 主因={dominant}'
    else:
        decision = 'keep'
        reason = f'退货率 {rate:.1%} 正常'

    fix_hint = {
        'quality': '联系供应商整改 / 换主供 / 临时下架',
        'size': '主图加尺寸图 / 标题加尺码 / 完善 specifics 维度',
    }.get(dominant, '继续观察')

    return {
        'sku': sku,
        'sold_30d': sold,
        'returned_30d': returned,
        'return_rate': round(rate, 4),
        'dominant_reason': dominant,
        'decision': decision,
        'reason': reason,
        'fix_hint': fix_hint,
    }


def batch_evaluate(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = [evaluate_sku(r) for r in rows]
    by_decision: Counter = Counter(it['decision'] for it in items)
    pause_candidates = [it for it in items if it['decision'] == 'pause']
    review_candidates = [it for it in items if it['decision'] == 'review']
    return {
        'items': items,
        'count': len(items),
        'by_decision': dict(by_decision),
        'pause_candidates': pause_candidates,
        'review_candidates': review_candidates,
    }
