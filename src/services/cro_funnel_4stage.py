"""S44 \u2014 \u5ba2\u6237\u884c\u4e3a\u6f0f\u6597\u7ec6\u5316.

\u5c06\u4f20\u7edf 3 \u9636\u6bb5 (impression \u2192 view \u2192 transaction) \u62c6\u6210 4 \u9636\u6bb5:
  impression \u2192 click \u2192 watch \u2192 transaction
\u67e5\u770b watch (\u5fc3\u613f\u5355) \u589e\u52a0\u4e2d\u95f4\u4fe1\u53f7, \u5212\u5206\u4e3a:
  - high_view_low_watch \u2192 \u4ef7\u683c/\u8fd0\u8d39\u8d29\u610f\u613f
  - high_watch_low_buy  \u2192 \u8d2d\u4e70\u51b3\u7b56\u5361\u70b9 (\u8bc4\u4ef7\u4e0d\u8db3 / \u9650\u65f6 / \u8d2d\u4e70\u4f53\u9a8c)
\u5982\u679c watch_count \u4e0d\u53ef\u83b7\u5f97 (None), \u540e\u9000\u5230\u7ecf\u5178 3 \u9636\u6bb5\u3002
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def classify(impressions: int,
             views: int,
             watches: Optional[int],
             transactions: int,
             ctr_low: float = 0.015,
             watch_rate_low: float = 0.20,
             buy_rate_low: float = 0.05,
             ) -> Dict[str, Any]:
    impressions = int(impressions or 0)
    views = int(views or 0)
    transactions = int(transactions or 0)
    ctr = (views / impressions) if impressions else 0.0
    cvr = (transactions / views) if views else 0.0
    if watches is None or watches < 0:
        # \u540e\u9000\u5230 3 \u6bb5
        if impressions == 0:
            stage = 'no_impression'
        elif ctr < ctr_low:
            stage = 'low_ctr'
        elif cvr < buy_rate_low:
            stage = 'low_cvr'
        else:
            stage = 'healthy'
        return {'stage': stage, 'detailed': False,
                'ctr': round(ctr, 4), 'cvr': round(cvr, 4)}
    watches = int(watches)
    watch_rate = (watches / views) if views else 0.0
    buy_rate = (transactions / watches) if watches else 0.0
    if impressions == 0:
        stage = 'no_impression'
    elif ctr < ctr_low:
        stage = 'low_ctr'
    elif watch_rate < watch_rate_low:
        stage = 'high_view_low_watch'
    elif buy_rate < buy_rate_low:
        stage = 'high_watch_low_buy'
    else:
        stage = 'healthy'
    return {
        'stage': stage,
        'detailed': True,
        'ctr': round(ctr, 4),
        'watch_rate': round(watch_rate, 4),
        'buy_rate': round(buy_rate, 4),
        'cvr': round(cvr, 4),
    }


def stage_to_action_hint(stage: str) -> str:
    return {
        'no_impression': '\u53ef\u89c1\u6027/promote\u51fa\u4ef7',
        'low_ctr': '\u4fee\u6b63\u4e3b\u56fe / \u6807\u9898 keyword',
        'high_view_low_watch': '\u4ef7\u683c\u504f\u9ad8 / \u8fd0\u8d39 / \u9996\u56fe\u5438\u5f15\u4e0d\u591f',
        'high_watch_low_buy': '\u8bc4\u4ef7/\u8be6\u60c5/\u9650\u65f6\u4f18\u60e0\u4fc3\u8f6c\u5316',
        'healthy': '\u4fdd\u6301',
    }.get(stage, '\u672a\u77e5')
