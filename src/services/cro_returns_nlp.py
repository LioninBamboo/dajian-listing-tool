"""S84 — returns 文本 NLP 分类.

退货理由文本 → bucket (size/quality/color/shipping/wrong_item/other).
关键词为主, llm_call 注入兜底 (None 时仅用关键词).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional

BUCKETS = ('size', 'quality', 'color', 'shipping', 'wrong_item', 'other')

# 中英文关键词映射
KEYWORD_RULES: Dict[str, List[str]] = {
    'size': ['size', 'small', 'large', 'fit', 'tight', 'loose',
             '尺寸', '大小', '太小', '太大', '不合身'],
    'quality': ['quality', 'broken', 'defect', 'damage', 'cracked',
                'poor', '质量', '坏', '破', '裂', '次品', '缺陷'],
    'color': ['color', 'colour', 'shade', 'different color',
              '颜色', '色差', '不一样'],
    'shipping': ['ship', 'late', 'arrived late', 'delivery', 'package',
                 '物流', '配送', '迟到', '晚到', '包装'],
    'wrong_item': ['wrong', 'not what i ordered', 'different item',
                   '错', '发错', '不是我买的', '货不对板'],
}


def classify_with_keywords(text: str) -> str:
    if not text or not isinstance(text, str):
        return 'other'
    lower = text.lower()
    for bucket, keywords in KEYWORD_RULES.items():
        for kw in keywords:
            if kw.lower() in lower:
                return bucket
    return 'other'


def classify_with_llm(text: str,
                      llm_call: Callable[[str], str],
                      ) -> str:
    """LLM 兜底: 让 llm_call 返回 bucket 名称, 不在 BUCKETS 内则 'other'."""
    prompt = (
        f'退货理由: {text}\n'
        f'请只回复以下分类之一: {", ".join(BUCKETS)}\n'
    )
    try:
        result = (llm_call(prompt) or '').strip().lower()
    except Exception:
        return 'other'
    for b in BUCKETS:
        if result.startswith(b):
            return b
    return 'other'


def classify_reason(text: str,
                    llm_call: Optional[Callable[[str], str]] = None,
                    ) -> Dict[str, Any]:
    """关键词命中即返; 关键词 'other' 且 llm 可用→走 LLM."""
    bucket = classify_with_keywords(text)
    source = 'keyword'
    if bucket == 'other' and llm_call is not None and text:
        llm_bucket = classify_with_llm(text, llm_call)
        if llm_bucket != 'other':
            bucket = llm_bucket
            source = 'llm'
    return {'bucket': bucket, 'source': source, 'text': text}


def classify_batch(texts: Iterable[str],
                   llm_call: Optional[Callable[[str], str]] = None,
                   ) -> Dict[str, Any]:
    by_bucket: Dict[str, int] = {b: 0 for b in BUCKETS}
    items: List[Dict[str, Any]] = []
    for t in texts:
        c = classify_reason(t, llm_call)
        items.append(c)
        by_bucket[c['bucket']] += 1
    total = len(items)
    return {
        'items': items,
        'by_bucket': by_bucket,
        'total': total,
        'top_bucket': max(by_bucket, key=by_bucket.get) if total else None,
    }
