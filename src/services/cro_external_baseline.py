"""S38 \u2014 \u8de8\u5e73\u53f0\u57fa\u7ebf.

\u5728 S35 \u591a\u6e90\u4ea4\u53c9\u6821\u9a8c\u4e4b\u4e0a, \u5f15\u5165 Amazon/Walmart \u540c SKU \u4f5c\u4e3a\u5916\u90e8\u57fa\u7ebf:
  - \u907f\u514d "eBay \u653f\u7b56\u53d8\u52a8 \u2192 \u6240\u6709 SKU CTR \u96ea\u5d29 \u2192 \u6211\u4eec\u9519\u8bf3\u4ef7\u8def\u4e0d\u597d" \u4f1a\u88ab\u8bef\u5224
  - \u5982\u679c eBay \u4e0b\u8dcc \u4e14 Amazon/Walmart \u540c\u671f\u4e5f\u4e0b\u8dcc \u2192 \u8be5 SKU \u4e0d\u662f\u5e73\u53f0\u95ee\u9898 (\u53d6\u6d88\u62a5\u8b66)
  - \u5982\u679c eBay \u4e0b\u8dcc \u4f46\u5916\u90e8\u4e0a\u6da8 \u2192 \u9ad8\u4f18\u5148\u7ea7 P0 \u95ee\u9898

\u5916\u90e8\u6570\u636e\u8c03\u7528\u8005\u6ce8\u5165 (\u4e0d\u52a0\u4f9d\u8d56). \u4f7f\u7528:
  classify_signal(internal_trend, external_trends) \u2192 dict
"""
from __future__ import annotations

from typing import Any, Dict, List

DROP_THRESHOLD = -0.10  # \u4e0b\u8dcc 10%
RISE_THRESHOLD = 0.05   # \u4e0a\u6da8 5%


def _direction(trend: float) -> str:
    if trend <= DROP_THRESHOLD:
        return 'down'
    if trend >= RISE_THRESHOLD:
        return 'up'
    return 'flat'


def classify_signal(sku: str,
                    internal_trend: float,
                    external_trends: Dict[str, float],
                    ) -> Dict[str, Any]:
    """internal_trend \u662f eBay \u8fd1 7d \u5bf9\u6bd4\u4e0a 7d CTR \u53d8\u5316\u7387.

    external_trends \u662f {platform: trend} (\u5982 {'amazon': -0.15}).
    """
    int_dir = _direction(internal_trend)
    ext_dirs = {p: _direction(t) for p, t in (external_trends or {}).items()}
    if int_dir != 'down':
        verdict = 'no_internal_drop'
        priority = 'low'
    elif not ext_dirs:
        verdict = 'no_external_baseline'
        priority = 'medium'
    elif all(d == 'down' for d in ext_dirs.values()):
        verdict = 'macro_drop'  # \u591a\u5e73\u53f0\u540c\u8dcc, \u975e eBay \u95ee\u9898
        priority = 'low'
    elif any(d == 'up' for d in ext_dirs.values()):
        verdict = 'platform_specific_drop'  # eBay \u8dcc / \u5916\u90e8\u6da8 \u2192 \u9ad8\u4f18\u5148\u7ea7
        priority = 'high'
    else:
        verdict = 'platform_likely_drop'
        priority = 'medium'
    return {
        'sku': sku,
        'internal_trend': round(internal_trend, 4),
        'internal_dir': int_dir,
        'external_dirs': ext_dirs,
        'verdict': verdict,
        'priority': priority,
    }


def batch_classify(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """records: [{sku, internal_trend, external_trends}]."""
    rows = [classify_signal(r['sku'], r['internal_trend'],
                            r.get('external_trends') or {}) for r in records]
    return {
        'evaluated': len(rows),
        'high_priority': [r for r in rows if r['priority'] == 'high'],
        'macro_drop_skus': [r['sku'] for r in rows if r['verdict'] == 'macro_drop'],
        'rows': rows,
    }
