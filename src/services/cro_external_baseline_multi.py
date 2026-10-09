"""S68 — 多平台 consensus 扩展, 不修改 S38 cro_external_baseline.

输入: 每 SKU 在多个平台 (amazon/walmart/shopify) 的 trend dict + eBay 内部 trend.
输出: consensus_drop / divergent_drop / platform_specific_drop 等更细 verdict.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

DROP = -0.10
RISE = 0.05
KNOWN_PLATFORMS = ('amazon', 'walmart', 'shopify', 'tiktok')
MIN_PLATFORMS_FOR_CONSENSUS = 2


def _direction(trend: Optional[float]) -> str:
    if trend is None:
        return 'unknown'
    if trend <= DROP:
        return 'drop'
    if trend >= RISE:
        return 'rise'
    return 'flat'


def classify_multi(sku: str,
                   internal_trend: Optional[float],
                   platforms: Dict[str, float],
                   ) -> Dict[str, Any]:
    int_dir = _direction(internal_trend)
    ext_dirs = {p: _direction(t) for p, t in platforms.items()}
    drops = [p for p, d in ext_dirs.items() if d == 'drop']
    rises = [p for p, d in ext_dirs.items() if d == 'rise']

    if int_dir != 'drop':
        verdict = 'no_internal_drop'
    elif not ext_dirs:
        verdict = 'no_external_baseline'
    elif len(drops) >= MIN_PLATFORMS_FOR_CONSENSUS:
        verdict = 'consensus_drop'  # 多平台同跌 → macro
    elif len(rises) >= MIN_PLATFORMS_FOR_CONSENSUS and not drops:
        verdict = 'platform_specific_drop'  # 别人涨, 我们跌 → 高优
    elif drops and rises:
        verdict = 'divergent_drop'  # 部分跌部分涨, 看具体业务
    elif drops:
        verdict = 'partial_drop'
    else:
        verdict = 'platform_likely_drop'

    return {
        'sku': sku,
        'internal_trend': internal_trend,
        'platforms': platforms,
        'platform_directions': ext_dirs,
        'drop_platforms': drops,
        'rise_platforms': rises,
        'verdict': verdict,
        'priority': _verdict_priority(verdict),
    }


def _verdict_priority(verdict: str) -> str:
    if verdict == 'platform_specific_drop':
        return 'high'
    if verdict in {'divergent_drop', 'partial_drop'}:
        return 'medium'
    return 'low'


def batch_classify_multi(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [
        classify_multi(r['sku'], r.get('internal_trend'),
                       r.get('platforms') or {})
        for r in records
    ]
    by_verdict: Dict[str, List[str]] = {}
    for r in rows:
        by_verdict.setdefault(r['verdict'], []).append(r['sku'])
    high = [r['sku'] for r in rows if r['priority'] == 'high']
    return {
        'total': len(rows),
        'rows': rows,
        'by_verdict': by_verdict,
        'high_priority_skus': high,
        'consensus_drop_skus': by_verdict.get('consensus_drop', []),
    }
