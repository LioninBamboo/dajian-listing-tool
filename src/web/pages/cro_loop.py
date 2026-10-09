"""S29 \u2014 CRO \u95ed\u73af\u770b\u677f.

\u4ee5 SKU \u4e3a\u4e3b\u8f74, \u4e32\u8054 diagnose \u2192 enqueue \u2192 done \u2192 effect_audit \u56db\u6b65\u73b0\u72b6.
\u53e6\u63d0\u4f9b\u9ed1\u540d\u5355\u589e/\u5220 \u5165\u53e3, \u8ba9\u8fd0\u8425\u4e00\u952e\u9a73\u56de\u8bef\u8bca SKU.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st  # noqa: F401  (import for streamlit page contract)

from src.services.cro_action_queue import DEFAULT_QUEUE
from src.services.cro_diagnose_blacklist import (
    add_to_blacklist, list_blacklist, remove_from_blacklist,
)
from src.services.cro_trace_view import (
    filter_by_kind, render_chips_html, severity_color,
)

SIGNAL_KIND_OPTIONS = (
    '全部', 'external', 'funnel', 'returns', 'inventory',
    'elasticity', 'roi', 'cohort', 'historical',
)


def _load_queue_rows() -> List[Dict[str, Any]]:
    qp = Path(DEFAULT_QUEUE)
    if not qp.exists():
        return []
    rows = []
    with qp.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_status = Counter(r.get('status', '') for r in rows)
    by_action = Counter(r.get('action', '') for r in rows)
    by_cohort = Counter(r.get('cohort', 'na') for r in rows)
    by_sku: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        sku = r.get('sku')
        if not sku:
            continue
        by_sku.setdefault(sku, []).append(r)
    return {
        'by_status': dict(by_status),
        'by_action': dict(by_action),
        'by_cohort': dict(by_cohort),
        'by_sku': by_sku,
        'total': len(rows),
    }


def render():  # pragma: no cover - streamlit UI
    st.title('\U0001F3AF CRO \u95ed\u73af\u770b\u677f')
    rows = _load_queue_rows()
    stats = _summarise(rows)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('\u603b\u52a8\u4f5c', stats['total'])
    c2.metric('\u5f85\u6267\u884c', stats['by_status'].get('pending', 0))
    c3.metric('\u5df2\u5b8c\u6210', stats['by_status'].get('done', 0))
    c4.metric('A/B \u5bf9\u7167\u7ec4', stats['by_cohort'].get('control', 0))

    st.subheader('\u6309\u52a8\u4f5c\u7c7b\u578b')
    st.bar_chart(stats['by_action'])

    st.subheader('SKU \u95ed\u73af\u660e\u7ec6')
    kind_pick = st.selectbox(
        '\u4fe1\u53f7\u8fc7\u6ee4', SIGNAL_KIND_OPTIONS, index=0,
        help='\u6309 reasoning_trace \u4e2d\u7684\u4fe1\u53f7\u79cd\u7c7b\u8fc7\u6ee4 SKU',
    )
    selected_kind = None if kind_pick == '\u5168\u90e8' else kind_pick
    filtered_rows = filter_by_kind(rows, selected_kind)
    filtered_sku_set = {r.get('sku') for r in filtered_rows if r.get('sku')}
    visible_sku_items = [
        (sku, rs) for sku, rs in stats['by_sku'].items()
        if (selected_kind is None) or (sku in filtered_sku_set)
    ]
    st.caption(f'\u5339\u914d {len(visible_sku_items)} SKU')
    for sku, rs in visible_sku_items[:50]:
        latest_trace: List[str] = []
        for r in reversed(rs):
            t = r.get('reasoning_trace') or []
            if t:
                latest_trace = t
                break
        bar = severity_color(latest_trace)
        header = (f'<span style="display:inline-block;width:6px;height:14px;'
                  f'background:{bar};margin-right:6px;border-radius:2px;'
                  f'vertical-align:middle;"></span>{sku} \u00b7 {len(rs)} \u6761')
        with st.expander(' ', expanded=False):
            st.markdown(header, unsafe_allow_html=True)
            st.markdown(render_chips_html(latest_trace),
                        unsafe_allow_html=True)
            for r in rs:
                st.json(r)
            if st.button(f'\u52a0\u5165\u9ed1\u540d\u5355: {sku}', key=f'bl-add-{sku}'):
                add_to_blacklist(sku, reason='manual via cro_loop')
                st.success(f'{sku} \u5df2\u52a0\u5165\u9ed1\u540d\u5355')

    st.subheader('\u5f53\u524d\u9ed1\u540d\u5355')
    bl = list_blacklist()
    st.dataframe(bl)
    for entry in bl[:20]:
        if st.button(f'\u79fb\u9664: {entry["sku"]}', key=f'bl-rm-{entry["sku"]}'):
            remove_from_blacklist(entry['sku'])
            st.info(f'{entry["sku"]} \u5df2\u79fb\u51fa\u9ed1\u540d\u5355')


if __name__ == '__main__':  # pragma: no cover
    render()
