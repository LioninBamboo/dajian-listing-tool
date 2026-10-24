"""S48 — 退货率反馈环.

输入: returns_fetcher() → [{sku, return_count, sold_count, reason_codes:[...] }]
输出: 高退货率 SKU + 推荐处置 (黑名单 / 内容审计 / size_chart_check)

不直接调 eBay returns API; returns_fetcher 由调用方注入。
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, List, Optional

HIGH_RETURN_RATE = 0.15
MIN_SOLD_FOR_JUDGE = 10

# eBay return reason codes → 我们的处置标签
SIZE_REASONS = {'NOT_AS_DESCRIBED_SIZE', 'WRONG_SIZE', 'TOO_SMALL', 'TOO_BIG'}
QUALITY_REASONS = {'DEFECTIVE', 'BROKEN', 'POOR_QUALITY'}
DESC_REASONS = {'NOT_AS_DESCRIBED', 'WRONG_ITEM'}


def _classify_reasons(reason_codes: List[str]) -> str:
    if not reason_codes:
        return 'unknown'
    cnt = Counter(reason_codes)
    top = cnt.most_common(1)[0][0]
    if top in SIZE_REASONS:
        return 'size_mismatch'
    if top in QUALITY_REASONS:
        return 'quality_defect'
    if top in DESC_REASONS:
        return 'desc_mismatch'
    return 'other'


def analyze_returns(returns_fetcher: Callable[[], List[Dict[str, Any]]],
                    high_rate: float = HIGH_RETURN_RATE,
                    min_sold: int = MIN_SOLD_FOR_JUDGE,
                    ) -> Dict[str, Any]:
    rows = returns_fetcher() or []
    high_return: List[Dict[str, Any]] = []
    skipped_low_volume = 0
    for r in rows:
        sold = r.get('sold_count', 0) or 0
        ret = r.get('return_count', 0) or 0
        if sold < min_sold:
            skipped_low_volume += 1
            continue
        rate = ret / sold if sold else 0
        if rate < high_rate:
            continue
        category = _classify_reasons(r.get('reason_codes', []) or [])
        high_return.append({
            'sku': r.get('sku'),
            'return_rate': round(rate, 3),
            'return_count': ret,
            'sold_count': sold,
            'reason_category': category,
            'recommendation': _recommend(category),
        })
    high_return.sort(key=lambda x: x['return_rate'], reverse=True)
    return {
        'high_return': high_return,
        'high_return_count': len(high_return),
        'skipped_low_volume': skipped_low_volume,
        'blacklist_skus': [r['sku'] for r in high_return
                           if r['reason_category'] in
                           ('quality_defect', 'desc_mismatch')],
    }


def _recommend(category: str) -> str:
    return {
        'size_mismatch': '审计 dimensions/size_chart, 校正 listing 尺寸描述',
        'quality_defect': '加入黑名单, 暂停 promote, 联系供应商',
        'desc_mismatch': '加入黑名单, 重写 description + 主图',
        'other': '人工抽查',
        'unknown': '人工抽查',
    }.get(category, '人工抽查')


def render_returns_brief(report: Dict[str, Any],
                         max_lines: int = 5) -> str:
    if not report.get('high_return'):
        return '退货率全部正常 ✓'
    lines = [
        f'⚠️ 高退货率 SKU {report["high_return_count"]} 个 '
        f'(skip 低销量 {report.get("skipped_low_volume", 0)})',
    ]
    for r in report['high_return'][:max_lines]:
        lines.append(
            f'  • {r["sku"]}: {r["return_rate"] * 100:.1f}% '
            f'({r["return_count"]}/{r["sold_count"]}) '
            f'→ {r["recommendation"]}'
        )
    return '\n'.join(lines)
