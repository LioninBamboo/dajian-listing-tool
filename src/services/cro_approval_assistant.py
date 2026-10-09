"""S42 \u2014 \u667a\u80fd\u5ba1\u6279\u52a9\u624b.

\u7ed9 S37 \u5ba1\u6279\u961f\u5217\u4e2d\u6bcf\u6761\u52a0\u4e00\u53e5 AI \u5efa\u8bae:
  delist        \u2192 \u9ad8\u4e0d\u6d3b\u8dc3 + \u6210\u672c\u9ad8 \u2192 \u5efa\u8bae\u6279\u51c6
  threshold     \u2192 \u6837\u672c\u8db3 (>=30) + \u53d8\u52a8\u5e45\u5ea6\u5c0f \u2192 \u5efa\u8bae\u6279\u51c6
  promote_rb    \u2192 ROI < 0.5 \u2192 \u5f3a\u70c8\u5efa\u8bae\u6279\u51c6 ; ROI 0.5-1.0 \u2192 \u89c2\u671b
LLM \u53ef\u9009 (\u4f20\u5165 llm_call \u589e\u5f3a). \u4e0d\u4f20 \u2192 \u7eaf\u89c4\u5219\u5efa\u8bae.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def _suggest_delist(item: Dict[str, Any]) -> Dict[str, Any]:
    return {'verdict': 'review',
            'reason': '\u4eba\u5de5\u5224\u65ad\u662f\u5426\u4e0b\u67b6 (\u4e0d\u53ef\u9006\u52a8\u4f5c)',
            'confidence': 0.5}


def _suggest_threshold(item: Dict[str, Any]) -> Dict[str, Any]:
    summary = item.get('summary') or ''
    n_idx = summary.find('n=')
    n = 0
    if n_idx >= 0:
        try:
            n = int(summary[n_idx+2:].rstrip(')').split()[0])
        except Exception:
            n = 0
    if n >= 30:
        return {'verdict': 'approve',
                'reason': f'\u6837\u672c\u8db3 (n={n}), \u53ef\u63a8\u4ea7',
                'confidence': 0.85}
    return {'verdict': 'wait',
            'reason': f'\u6837\u672c\u4e0d\u8db3 (n={n}<30), \u518d\u7d2f\u79ef',
            'confidence': 0.7}


def _suggest_rollback(item: Dict[str, Any]) -> Dict[str, Any]:
    summary = item.get('summary') or ''
    roi = None
    if 'ROI=' in summary:
        try:
            roi = float(summary.split('ROI=')[1].split()[0])
        except Exception:
            roi = None
    if roi is not None and roi < 0.5:
        return {'verdict': 'approve',
                'reason': f'ROI={roi} \u4e25\u91cd\u4e0d\u76c8\u5229, \u5efa\u8bae\u56de\u8c03',
                'confidence': 0.95}
    if roi is not None and roi < 1.0:
        return {'verdict': 'review',
                'reason': f'ROI={roi} \u8fb9\u7f18, \u518d\u89c2\u5bdf 3-7 \u5929',
                'confidence': 0.6}
    return {'verdict': 'wait',
            'reason': '\u6570\u636e\u4e0d\u8db3, \u4fdd\u6301\u73b0\u72b6',
            'confidence': 0.4}


_SUGGEST = {
    'delist': _suggest_delist,
    'threshold_promote': _suggest_threshold,
    'promote_rollback': _suggest_rollback,
}


def annotate(items: List[Dict[str, Any]],
             llm_call: Optional[Callable[[Dict[str, Any]], str]] = None,
             ) -> List[Dict[str, Any]]:
    out = []
    for item in items:
        kind = item.get('kind')
        fn = _SUGGEST.get(kind)
        sugg = fn(item) if fn else {'verdict': 'review',
                                    'reason': 'unknown kind',
                                    'confidence': 0.0}
        if llm_call is not None:
            try:
                sugg['llm_note'] = llm_call(item)
            except Exception as e:
                sugg['llm_note'] = f'(llm error: {e})'
        new = dict(item)
        new['suggestion'] = sugg
        out.append(new)
    return out


def annotate_report(report: Dict[str, Any],
                    llm_call: Optional[Callable] = None,
                    ) -> Dict[str, Any]:
    new = dict(report)
    new['items'] = annotate(report.get('items') or [], llm_call=llm_call)
    new['auto_approve_count'] = sum(
        1 for i in new['items']
        if i['suggestion']['verdict'] == 'approve'
    )
    return new
