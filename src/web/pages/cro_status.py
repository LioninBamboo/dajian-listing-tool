"""S51 — CRO 状态总览 (traffic-light) Streamlit 页.

调用 src.services.cro_dashboard_collectors + cro_dashboard_traffic_light, 5 个 pillar
彩色卡片, 30 秒自动刷新。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

import streamlit as st

from src.services.cro_dashboard_collectors import (
    collect_alerts, collect_approvals, collect_inventory, collect_returns,
)
from src.services.cro_dashboard_traffic_light import (
    build_dashboard, render_traffic_light,
)

ALERTS_PATH = Path('logs/cro_alerts.jsonl')
RETURNS_PATH = Path('logs/cro_returns.jsonl')
INV_PATH = Path('logs/cro_replenish_alerts.jsonl')
OPS_SNAPSHOT_PATH = Path('logs/cro_ops_snapshot.json')

COLOR = {'green': '#22c55e', 'yellow': '#facc15', 'red': '#ef4444'}


def _load_ops_snapshot(path: Path = OPS_SNAPSHOT_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {'overall_status': 'red', 'reasons': ['snapshot unreadable']}


def _render_ops_snapshot(snapshot: Dict[str, Any]) -> None:
    st.subheader('CRO Ops 快照')
    if not snapshot:
        st.info('尚未生成 ops 快照。可运行 python scheduler_daemon.py --task cro_ops')
        return

    status = str(snapshot.get('overall_status') or 'yellow').lower()
    color = COLOR.get(status, COLOR['yellow'])
    st.markdown(
        f'<div style="padding:10px;border-radius:8px;background:{color};'
        f'color:white;font-size:18px;font-weight:600;">'
        f'Ops: {status.upper()} · {snapshot.get("generated_at", "")}'
        f'</div>',
        unsafe_allow_html=True,
    )
    reasons = snapshot.get('reasons') or []
    if reasons:
        st.warning(' · '.join(str(item) for item in reasons))

    sections = snapshot.get('sections') or {}
    capacity = sections.get('capacity') or {}
    dr = sections.get('disaster_recovery') or {}
    compliance = sections.get('compliance') or {}
    improvement = sections.get('continuous_improvement') or {}

    c1, c2, c3, c4 = st.columns(4)
    c1.metric('容量等级', capacity.get('worst_severity', capacity.get('status', '—')))
    c2.metric('DR Drill', dr.get('status', '—'))
    c3.metric('合规缺口', int(compliance.get('missing_legal_basis', 0) or 0))
    c4.metric('改进建议', int(improvement.get('suggestion_count', 0) or 0))

    with st.expander('容量详情 (Capacity Details)'):
        if capacity.get('status') == 'skipped':
            st.info(f"Skipped: {capacity.get('reason')}")
        else:
            by_comp = capacity.get('by_component', {})
            if not by_comp:
                st.info("No component data available.")
            else:
                rows = []
                for comp, stats in by_comp.items():
                    rows.append({
                        "组件": comp,
                        "状态": stats.get('status', ''),
                        "使用率": f"{stats.get('used_pct', 0):.2%}",
                        "剩余天数": round(stats.get('days_until_full', 0), 1) if stats.get('days_until_full') is not None else '-',
                        "满载日期": stats.get('eta', '')[:10] if stats.get('eta') else '-',
                        "严重级别": stats.get('severity', '')
                    })
                st.dataframe(rows, use_container_width=True)

    with st.expander('DR Drill 详情 (Disaster Recovery)'):
        if dr.get('status') == 'skipped':
            st.info(f"Skipped: {dr.get('reason')}")
        else:
            st.json(dr)

    with st.expander('合规缺口详情 (Compliance Details)'):
        if compliance.get('status') == 'skipped':
            st.info(f"Skipped: {compliance.get('reason')} - {compliance.get('path', '')}")
        else:
            st.json(compliance)

    brief = improvement.get('weekly_brief')
    if brief:
        with st.expander('周度改进摘要 (Weekly Improvement Brief)'):
            st.markdown(brief)
            suggestions = improvement.get('suggestions', [])
            if suggestions:
                st.markdown("#### 具体建议")
                st.json(suggestions)


def render(set_page_config: bool = True) -> None:
    if set_page_config:
        st.set_page_config(page_title='CRO 状态', layout='wide')
    st.title('🚦 CRO 状态总览')

    auto_refresh = st.sidebar.checkbox('30 秒自动刷新', value=False)
    if auto_refresh:
        st.sidebar.write(f'更新于 {time.strftime("%H:%M:%S")}')

    rep = build_dashboard(
        approvals_fetcher=lambda: collect_approvals(),
        alerts_fetcher=lambda: collect_alerts(ALERTS_PATH),
        returns_fetcher=lambda: collect_returns(RETURNS_PATH),
        inventory_fetcher=lambda: collect_inventory(INV_PATH),
    )

    overall_color = COLOR[rep['status_overall']]
    st.markdown(
        f'<div style="padding:12px;border-radius:8px;background:{overall_color};'
        f'color:white;font-size:20px;font-weight:600;">'
        f'整体: {rep["status_overall"].upper()} — {rep["recommended_action"]}'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.write('')

    cols = st.columns(len(rep['pillars']))
    for col, p in zip(cols, rep['pillars']):
        c = COLOR[p['status']]
        with col:
            st.markdown(
                f'<div style="padding:16px;border-radius:8px;background:{c};'
                f'color:white;text-align:center;">'
                f'<div style="font-size:14px;opacity:0.85;">{p["name"]}</div>'
                f'<div style="font-size:28px;font-weight:700;">{p["detail_count"]}</div>'
                f'<div style="font-size:13px;">{p["headline"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    with st.expander('文本摘要 (可复制)'):
        st.code(render_traffic_light(rep), language='text')

    st.markdown('---')
    _render_ops_snapshot(_load_ops_snapshot())

    if auto_refresh:
        time.sleep(30)
        st.rerun()


render_cro_status = render


if __name__ == '__main__':  # pragma: no cover
    render()
