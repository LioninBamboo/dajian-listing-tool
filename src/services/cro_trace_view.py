"""S63 — 给 cro_loop 页用的 trace 渲染 helper.

把 reasoning_trace 字符串列表 (e.g. ['external:macro_drop','funnel:low_ctr'])
转成 markdown chips, 让 cro_loop.py 调用 st.markdown(html, unsafe_allow_html=True).
"""
from __future__ import annotations

import html as _html
from typing import Dict, List, Optional

# 信号种类 → chip 颜色 (bg, fg)
CHIP_COLORS: Dict[str, str] = {
    'external': '#fee2e2',     # 红
    'funnel': '#fef3c7',       # 黄
    'returns': '#fce7f3',      # 粉
    'inventory': '#dbeafe',    # 蓝
    'elasticity': '#dcfce7',   # 绿
    'roi': '#e0e7ff',          # 紫
    'cohort': '#f3e8ff',       # 浅紫
    'historical': '#f1f5f9',   # 灰
}
DEFAULT_CHIP = '#f1f5f9'


def _chip_html(kind: str, value: str) -> str:
    color = CHIP_COLORS.get(kind, DEFAULT_CHIP)
    safe_kind = _html.escape(kind)
    safe_value = _html.escape(value)
    return (
        f'<span style="display:inline-block;'
        f'padding:2px 8px;margin:2px;border-radius:10px;'
        f'background:{color};color:#1f2937;font-size:12px;">'
        f'<b>{safe_kind}</b>:{safe_value}</span>'
    )


def render_chips_html(trace: List[str]) -> str:
    if not trace:
        return ('<span style="color:#9ca3af;font-size:12px;">'
                '无显式信号</span>')
    parts = []
    for entry in trace:
        if ':' in entry:
            kind, value = entry.split(':', 1)
        else:
            kind, value = 'other', entry
        parts.append(_chip_html(kind, value))
    return ''.join(parts)


def group_by_kind(trace: List[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for entry in trace:
        if ':' not in entry:
            out.setdefault('other', []).append(entry)
            continue
        kind, value = entry.split(':', 1)
        out.setdefault(kind, []).append(value)
    return out


def severity_color(trace: List[str]) -> str:
    """根据 trace 中是否含 external/funnel 高优信号返回左侧色条颜色."""
    kinds = {e.split(':', 1)[0] for e in trace if ':' in e}
    if 'external' in kinds:
        return '#ef4444'
    if 'funnel' in kinds or 'returns' in kinds:
        return '#facc15'
    if not kinds:
        return '#9ca3af'
    return '#22c55e'


def filter_by_kind(rows: List[Dict],
                   kind: Optional[str]) -> List[Dict]:
    """cro_loop 页支持按信号种类过滤. kind=None → 全返回."""
    if not kind:
        return rows
    out = []
    for r in rows:
        trace = r.get('reasoning_trace') or []
        if any(e.startswith(f'{kind}:') for e in trace):
            out.append(r)
    return out
