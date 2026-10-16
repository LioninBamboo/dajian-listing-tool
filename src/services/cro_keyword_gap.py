"""S119 — 竞品标题关键词差距分析.

输入: my_title, competitor_titles=[str, ...]
输出: 竞品标题里 *普遍出现* 但我没有的关键词 → 推荐补全.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List

TOKEN_RE = re.compile(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fa5]+")
STOP = {
    'the', 'a', 'an', 'and', 'or', 'for', 'with', 'in', 'on', 'of',
    'to', 'by', 'as', 'is', 'it', 'this', 'that',
    '的', '了', '是', '和', '与',
}


def tokenize(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in TOKEN_RE.findall(text) if t.lower() not in STOP]


def competitor_term_frequency(
    competitor_titles: Iterable[str],
) -> Counter:
    c: Counter = Counter()
    for t in competitor_titles:
        for w in set(tokenize(t)):
            c[w] += 1
    return c


def analyze_keyword_gap(my_title: str,
                         competitor_titles: List[str],
                         *,
                         min_competitor_share: float = 0.40,
                         max_recommendations: int = 5,
                         ) -> Dict[str, Any]:
    n = len(competitor_titles)
    if n == 0:
        return {'gap_terms': [], 'overlap_terms': [],
                'overlap_ratio': 0.0, 'competitor_count': 0}
    my_tokens = set(tokenize(my_title))
    freq = competitor_term_frequency(competitor_titles)
    threshold = max(1, int(round(n * min_competitor_share)))
    common_terms = {w for w, c in freq.items() if c >= threshold}
    gap = sorted(
        [(w, freq[w]) for w in common_terms if w not in my_tokens],
        key=lambda x: -x[1],
    )
    overlap = sorted(common_terms & my_tokens)
    overlap_ratio = (len(overlap) / len(common_terms)) if common_terms else 0.0
    return {
        'competitor_count': n,
        'common_threshold': threshold,
        'overlap_terms': overlap,
        'overlap_ratio': round(overlap_ratio, 4),
        'gap_terms': [{'term': w, 'competitor_count': c}
                       for w, c in gap[:max_recommendations]],
        'recommended_inserts': [w for w, _ in gap[:max_recommendations]],
    }


def batch_analyze(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """rows: [{sku, my_title, competitor_titles:[..]}]"""
    items = []
    high_gap = 0
    for r in rows:
        out = analyze_keyword_gap(r.get('my_title', ''),
                                    r.get('competitor_titles') or [])
        out['sku'] = r.get('sku')
        items.append(out)
        if out['overlap_ratio'] < 0.50 and out['competitor_count'] >= 3:
            high_gap += 1
    return {'items': items, 'count': len(items), 'high_gap_count': high_gap}
