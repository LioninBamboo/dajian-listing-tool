"""S46 — 长尾 SKU 归并建议.

目标: 30 天销量 < 阈值的 SKU 中, 找出同 ASIN 或同标题前缀下还有"主销" SKU 的,
推荐人工合并 (转量到主 SKU). 不自动下架, 只产出建议清单。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

LONGTAIL_SOLD_30D_MAX = 1
TITLE_PREFIX_LEN = 30


def _key_for(row: Dict[str, Any]) -> Optional[str]:
    asin = (row.get('asin') or '').strip()
    if asin:
        return f'asin:{asin}'
    title = (row.get('title') or '').strip()
    if len(title) >= TITLE_PREFIX_LEN:
        return f'title:{title[:TITLE_PREFIX_LEN].lower()}'
    return None


def suggest_merges(rows: Iterable[Dict[str, Any]],
                   sold_max: int = LONGTAIL_SOLD_30D_MAX,
                   ) -> Dict[str, Any]:
    """rows: [{sku, title, asin, sold_30d, ...}]. 输出 merge_groups + orphan_longtails."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    orphans: List[Dict[str, Any]] = []
    for r in rows:
        k = _key_for(r)
        if k is None:
            if (r.get('sold_30d') or 0) <= sold_max:
                orphans.append({'sku': r.get('sku'), 'reason': 'no_key'})
            continue
        groups[k].append(r)

    merge_groups: List[Dict[str, Any]] = []
    for key, members in groups.items():
        sorted_m = sorted(members, key=lambda x: x.get('sold_30d') or 0,
                          reverse=True)
        primary = sorted_m[0]
        primary_sold = primary.get('sold_30d') or 0
        longtails = [m for m in sorted_m[1:]
                     if (m.get('sold_30d') or 0) <= sold_max]
        if primary_sold > sold_max and longtails:
            merge_groups.append({
                'key': key,
                'primary_sku': primary.get('sku'),
                'primary_sold_30d': primary_sold,
                'longtail_skus': [m.get('sku') for m in longtails],
                'longtail_count': len(longtails),
            })
        else:
            for m in members:
                if (m.get('sold_30d') or 0) <= sold_max:
                    orphans.append({'sku': m.get('sku'),
                                    'reason': 'no_primary_in_group'})
    merge_groups.sort(key=lambda g: g['longtail_count'], reverse=True)
    return {
        'merge_groups': merge_groups,
        'orphan_longtails': orphans,
        'total_longtail': sum(g['longtail_count'] for g in merge_groups)
        + len(orphans),
    }
