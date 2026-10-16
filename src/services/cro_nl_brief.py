"""S39 \u2014 \u81ea\u7136\u8bed\u8a00\u8bca\u65ad\u62a5\u544a.

\u628a monthly_report (dict) \u538b\u7f29\u6210 1 \u9875\u4e2d\u6587\u6d1e\u5bdf:
  - \u8003\u8651 effect_audit / threshold suggest / sentinel / promote ROI \u52a0\u603b
  - \u8c03\u7528 LLM \u751f\u6210 1 \u9875 markdown
  - LLM \u8c03\u7528\u8005\u6ce8\u5165 \u2192 \u53ef\u6d4b

\u8fd9\u91cc\u4e0d\u76f4\u63a5\u8c03\u7528 qwen_optimizer.QwenOptimizer (\u907f\u514d\u4e3a\u4e86\u6d4b\u8bd5\u62c9\u8d77\u91cd\u4f9d\u8d56),
\u63d0\u4f9b _build_prompt + summarize_with_llm. \u4ea7\u73af\u5883\u5728 cron \u91cc adapter \u8fde qwen.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional


def _top(items, n):
    return list(items)[:n]


def build_prompt(report: Dict[str, Any]) -> str:
    """\u7ed3\u6784\u5316\u62a5\u544a\u538b\u7f29\u6210 LLM \u63d0\u793a\u8bcd."""
    summary = report.get('summary') or {}
    suggestions = report.get('suggestions') or []
    rollback = report.get('rollback_candidates') or []
    sentinel = report.get('sentinel_alerts') or []

    lines = [
        '\u4f60\u662f eBay \u8fd0\u8425\u52a9\u624b\u3002\u4e0b\u9762\u662f\u4e0a\u6708 CRO \u5173\u952e\u6307\u6807\u3002',
        '\u8bf7\u7528\u4e2d\u6587\u8f93\u51fa <= 200 \u5b57\u7684\u6d1e\u5bdf, \u6837\u5f0f:',
        '## \u4e00\u53e5\u8bdd\u603b\u7ed3',
        '## Top 3 \u884c\u52a8\u9879 (\u6309\u4f18\u5148\u7ea7)',
        '## \u9700\u8b66\u60d5\u7684\u98ce\u9669',
        '',
        '\u6307\u6807:',
        f"- \u8bc4\u4f30\u52a8\u4f5c {summary.get('total_evaluated', 0)} \u4e2a",
        f"- improved {summary.get('improved', 0)}, flat {summary.get('flat', 0)}, "
        f"worsened {summary.get('worsened', 0)}",
        f"- \u9608\u503c\u5efa\u8bae {len(suggestions)} \u6761",
        f"- promote ROI<1 SKU {len(rollback)} \u4e2a",
        f"- sentinel \u544a\u8b66 {len(sentinel)} \u4e2a",
        '',
        '\u9608\u503c\u5efa\u8bae\u524d 5 \u6761:',
        json.dumps(_top(suggestions, 5), ensure_ascii=False),
        '\u9700\u56de\u9000\u63a8\u5e7f\u524d 5:',
        json.dumps(_top(rollback, 5), ensure_ascii=False),
        '\u8fd1\u671f\u544a\u8b66\u524d 5:',
        json.dumps(_top(sentinel, 5), ensure_ascii=False),
    ]
    return '\n'.join(lines)


def summarize_with_llm(report: Dict[str, Any],
                       llm_call: Callable[[str], str],
                       ) -> str:
    """llm_call(prompt) \u8fd4\u56de markdown \u6587\u672c."""
    prompt = build_prompt(report)
    try:
        out = llm_call(prompt)
    except Exception as e:
        return f"## \u751f\u6210\u5931\u8d25\n\n\u539f\u56e0: {e}\n\n\u8bf7\u67e5\u9605\u539f\u59cb monthly_report.json\u3002"
    if not out or not out.strip():
        return "## (LLM \u8fd4\u56de\u4e3a\u7a7a)"
    return out.strip()


def fallback_summary(report: Dict[str, Any]) -> str:
    """\u4e0d\u4f9d\u8d56 LLM \u7684\u5360\u4f4d\u62a5\u544a (\u7528\u4e8e LLM \u4e0d\u53ef\u7528\u7684\u573a\u666f)."""
    s = report.get('summary') or {}
    sg = report.get('suggestions') or []
    rb = report.get('rollback_candidates') or []
    parts = ['## \u4e00\u53e5\u8bdd\u603b\u7ed3',
             f"\u4e0a\u6708\u8bc4\u4f30 {s.get('total_evaluated', 0)} \u4e2a CRO \u52a8\u4f5c, "
             f"{s.get('improved', 0)} \u8d77\u6548 / {s.get('worsened', 0)} \u53cd\u8f6c\u3002"]
    parts.append('## Top 3 \u884c\u52a8\u9879')
    if sg:
        for i, x in enumerate(_top(sg, 3), 1):
            parts.append(f"{i}. {x.get('category_id', '?')} "
                         f"\u9608\u503c {x.get('metric', '')} \u8c03\u4e3a "
                         f"{x.get('suggested', '?')}")
    else:
        parts.append('- \u65e0\u9608\u503c\u5efa\u8bae')
    parts.append('## \u9700\u8b66\u60d5\u7684\u98ce\u9669')
    if rb:
        for x in _top(rb, 3):
            parts.append(f"- {x.get('sku')} ROI={x.get('roi')}, \u5efa\u8bae\u56de\u8c03 promote")
    else:
        parts.append('- \u672a\u53d1\u73b0\u4e25\u91cd\u98ce\u9669')
    return '\n'.join(parts)
