"""S114 — 标题与买家搜索词匹配度评分.

输入: title, top_queries=[{query, impressions, clicks}]
输出: coverage(覆盖率) + 缺失高流量词 + 推荐插入词.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List

TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fa5]+")
EBAY_TITLE_MAX = 80


def tokenize_lower(text: str) -> List[str]:
    if not text:
        return []
    return [t.lower() for t in TOKEN_RE.findall(text)]


def query_token_set(query: str) -> set:
    return {t for t in tokenize_lower(query) if len(t) >= 2}


def score_title_match(title: str,
                       top_queries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    title_tokens = set(tokenize_lower(title))
    queries = list(top_queries or [])
    if not queries:
        return {
            'coverage': 0.0,
            'covered_impressions': 0,
            'total_impressions': 0,
            'missing_terms': [],
            'matched_terms': [],
            'title_length': len(title or ''),
        }
    total_imp = sum(int(q.get('impressions', 0) or 0) for q in queries)
    covered_imp = 0
    missing_weight: Counter = Counter()
    matched: set = set()
    for q in queries:
        tokens = query_token_set(q.get('query', ''))
        if not tokens:
            continue
        imp = int(q.get('impressions', 0) or 0)
        # 完整命中 (所有 token 都在标题里)
        if tokens.issubset(title_tokens):
            covered_imp += imp
            matched.update(tokens)
        else:
            for t in tokens - title_tokens:
                missing_weight[t] += imp
    coverage = (covered_imp / total_imp) if total_imp > 0 else 0.0
    missing_terms = [
        {'term': t, 'missing_impressions': c}
        for t, c in missing_weight.most_common(10)
    ]
    return {
        'coverage': round(coverage, 4),
        'covered_impressions': covered_imp,
        'total_impressions': total_imp,
        'missing_terms': missing_terms,
        'matched_terms': sorted(matched),
        'title_length': len(title or ''),
    }


def recommend_title_inserts(score: Dict[str, Any],
                             *,
                             top_n: int = 3,
                             max_extra_chars: int = None) -> List[str]:
    """从 missing_terms 取 top_n, 不超过预算字符数."""
    if max_extra_chars is None:
        max_extra_chars = max(0, EBAY_TITLE_MAX - int(score.get('title_length', 0)))
    out: List[str] = []
    used = 0
    for m in score.get('missing_terms', []):
        term = m['term']
        cost = len(term) + 1   # +1 for space
        if used + cost > max_extra_chars:
            continue
        out.append(term)
        used += cost
        if len(out) >= top_n:
            break
    return out


def batch_score(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = []
    low_coverage = 0
    for r in rows:
        out = score_title_match(r.get('title', ''),
                                  r.get('top_queries') or [])
        out['sku'] = r.get('sku')
        out['recommended_inserts'] = recommend_title_inserts(out)
        items.append(out)
        if out['coverage'] < 0.50 and out['total_impressions'] > 0:
            low_coverage += 1
    return {'items': items, 'count': len(items),
            'low_coverage_count': low_coverage}
