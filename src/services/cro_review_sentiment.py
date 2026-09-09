"""S96 — 评论情感分析.

关键词为主, llm_call 兜底. 输出 sentiment ∈ {positive,negative,neutral}
+ score [-1, 1].
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional

POS_WORDS = ['great', 'excellent', 'love', 'perfect', 'awesome', 'good',
             'best', 'recommend', 'amazing', 'wonderful',
             '好', '棒', '满意', '推荐', '很赞', '不错']
NEG_WORDS = ['bad', 'terrible', 'awful', 'broken', 'damaged', 'wrong',
             'horrible', 'hate', 'poor', 'worst', 'disappointed',
             '差', '坏', '烂', '不好', '失望', '退货', '骗']


def classify_with_keywords(text: str) -> Dict[str, Any]:
    if not text or not isinstance(text, str):
        return {'sentiment': 'neutral', 'score': 0.0,
                'pos_hits': 0, 'neg_hits': 0}
    lower = text.lower()
    pos = sum(1 for w in POS_WORDS if w.lower() in lower)
    neg = sum(1 for w in NEG_WORDS if w.lower() in lower)
    raw = pos - neg
    total = pos + neg
    score = (raw / total) if total > 0 else 0.0
    if score > 0.2:
        sentiment = 'positive'
    elif score < -0.2:
        sentiment = 'negative'
    else:
        sentiment = 'neutral'
    return {'sentiment': sentiment, 'score': round(score, 4),
            'pos_hits': pos, 'neg_hits': neg}


def classify_with_llm(text: str,
                      llm_call: Callable[[str], str]) -> Dict[str, Any]:
    prompt = (f'评论: {text}\n'
              f'只回复 positive / negative / neutral 之一.')
    try:
        result = (llm_call(prompt) or '').strip().lower()
    except Exception:
        return {'sentiment': 'neutral', 'score': 0.0, 'source': 'llm_error'}
    for s in ('positive', 'negative', 'neutral'):
        if result.startswith(s):
            score_map = {'positive': 0.8, 'negative': -0.8, 'neutral': 0.0}
            return {'sentiment': s, 'score': score_map[s], 'source': 'llm'}
    return {'sentiment': 'neutral', 'score': 0.0, 'source': 'llm_unknown'}


def analyze_review(text: str,
                   *,
                   llm_call: Optional[Callable[[str], str]] = None,
                   ) -> Dict[str, Any]:
    kw = classify_with_keywords(text)
    if (kw['pos_hits'] + kw['neg_hits']) == 0 and llm_call is not None:
        llm = classify_with_llm(text, llm_call)
        return {**llm, 'text': text, 'source': llm.get('source', 'llm')}
    return {**kw, 'text': text, 'source': 'keyword'}


def aggregate(reviews: Iterable[str],
              *,
              llm_call: Optional[Callable[[str], str]] = None,
              ) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    counts = {'positive': 0, 'negative': 0, 'neutral': 0}
    score_total = 0.0
    for r in reviews:
        a = analyze_review(r, llm_call=llm_call)
        items.append(a)
        counts[a['sentiment']] += 1
        score_total += a['score']
    n = len(items)
    avg = score_total / n if n else 0.0
    return {
        'items': items,
        'count': n,
        'by_sentiment': counts,
        'avg_score': round(avg, 4),
        'positive_pct': round(counts['positive'] / n, 4) if n else 0.0,
        'negative_pct': round(counts['negative'] / n, 4) if n else 0.0,
    }
