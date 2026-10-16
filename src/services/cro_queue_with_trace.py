"""S59 — 把 reasoning trace 注入 cro_action_queue 入队流程.

不修改原 enqueue, 提供一个包装 enqueue_with_trace(actions, signal_lookup):
1) 用 S54 attach_trace 给每条 action 写 reasoning_trace + reasoning_summary
2) 转交原 enqueue 落盘

cro_loop 页可调 read_traces_for_pending() 直接显示给审批人。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from src.services.cro_action_queue import DEFAULT_QUEUE, enqueue, load_pending
from src.services.cro_reasoning_trace import attach_trace


def enqueue_with_trace(
    actions: Iterable[Dict[str, Any]],
    signal_lookup: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    queue_path: Optional[Path] = None,
    source: str = 'cro_diagnoser',
) -> int:
    """每条 action 入队前调 attach_trace.

    signal_lookup(action)→signals dict; 缺省取 action.get('signals',{}).
    """
    annotated: List[Dict[str, Any]] = []
    for a in actions:
        sigs = (signal_lookup(a) if signal_lookup
                else a.get('signals', {})) or {}
        annotated.append(attach_trace(a, sigs))
    return enqueue(annotated, queue_path=queue_path, source=source)


def read_traces_for_pending(queue_path: Optional[Path] = None,
                            ) -> List[Dict[str, Any]]:
    """给 cro_loop 页用: 列出所有 pending 条目的 sku/action/trace/summary."""
    rows = load_pending(queue_path=queue_path)
    out = []
    for r in rows:
        out.append({
            'sku': r.get('sku'),
            'action': r.get('action'),
            'reasoning_trace': r.get('reasoning_trace') or [],
            'reasoning_summary': r.get('reasoning_summary')
            or '无显式信号',
        })
    return out


def render_trace_text(rows: List[Dict[str, Any]],
                      max_lines: int = 20) -> str:
    if not rows:
        return '队列空'
    lines = []
    for r in rows[:max_lines]:
        lines.append(
            f'• {r.get("sku")} [{r.get("action")}] → '
            f'{r.get("reasoning_summary")}'
        )
    return '\n'.join(lines)
