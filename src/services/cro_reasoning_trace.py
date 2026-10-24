"""S54 — 决策可解释性 (reasoning trace).

给 CRO action 附 reasoning_trace 字段:
  ["funnel:high_view_low_watch", "external:macro_drop", "elasticity:elastic"]

让审批人不用查多个面板就能判断为什么这条 action 被生成。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 预设信号优先级 (越靠前越重要)
SIGNAL_ORDER = (
    'external', 'funnel', 'returns', 'inventory',
    'elasticity', 'roi', 'cohort', 'historical',
)


def build_trace(signals: Dict[str, Any]) -> List[str]:
    """signals 形如 {'funnel': 'high_view_low_watch', 'roi': 0.4, ...}.

    输出按 SIGNAL_ORDER 排序的 'kind:value' 字符串列表。
    """
    items: List[tuple] = []
    for kind, value in signals.items():
        if value is None or value == '':
            continue
        items.append((kind, value))
    rank = {k: i for i, k in enumerate(SIGNAL_ORDER)}
    items.sort(key=lambda kv: rank.get(kv[0], 999))
    return [f'{k}:{_fmt(v)}' for k, v in items]


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f'{v:.2f}'
    return str(v)


def attach_trace(action: Dict[str, Any], signals: Dict[str, Any],
                 ) -> Dict[str, Any]:
    action = dict(action)
    action['reasoning_trace'] = build_trace(signals)
    action['reasoning_summary'] = summarize_trace(action['reasoning_trace'])
    return action


def summarize_trace(trace: List[str], max_chars: int = 80) -> str:
    if not trace:
        return '无显式信号'
    joined = ' + '.join(trace)
    return joined if len(joined) <= max_chars else joined[: max_chars - 1] + '…'


def attach_traces_bulk(
    actions: List[Dict[str, Any]],
    signal_lookup: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """signal_lookup(action) -> dict; 缺省取 action.get('signals', {})."""
    out = []
    for a in actions:
        sigs = (signal_lookup(a) if signal_lookup is not None
                else a.get('signals', {})) or {}
        out.append(attach_trace(a, sigs))
    return out


def has_signal(action: Dict[str, Any], kind: str) -> bool:
    for entry in action.get('reasoning_trace') or []:
        if entry.startswith(f'{kind}:'):
            return True
    return False
