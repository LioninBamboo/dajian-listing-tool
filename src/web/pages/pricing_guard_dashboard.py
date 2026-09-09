"""价格守门员仪表盘 (P6, 2026-05).

单页面汇总最新一天的:
  - 拒绝改价 / 触底关广告 (来自 logs)
  - 广告恢复 (来自最新 ad_restore_audit_*.json)
  - bid 调整 (来自最新 batch_smart_bid_*.json)
  - bid 回溯 (来自最新 bid_rollback_audit_*.json)
  - 广告黑名单 (来自 logs/ad_blacklist.json)

也提供手动管理: 加/移除黑名单 SKU.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = PROJECT_ROOT / 'logs'


def _latest_json(prefix: str) -> Optional[Path]:
    if not LOG_DIR.exists():
        return None
    matches = sorted(LOG_DIR.glob(f'{prefix}_*.json'), reverse=True)
    return matches[0] if matches else None


def _read_json(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def render_pricing_guard_dashboard():
    st.title("🛡️ 价格守门员仪表盘")
    st.caption("汇总当日 / 最新一次的守门员动作: 拒改 · 关广告 · 恢复广告 · Bid 调整 · 黑名单")

    # ─── 当日活动 ─────────────────────────────────────
    try:
        from src.services.guard_activity import collect_guard_activity
        activity = collect_guard_activity()
    except Exception as exc:
        st.error(f"加载守门员活动失败: {exc}")
        activity = {'rejects': [], 'ad_offs': [], 'restore_ok': 0,
                    'restore_failed': 0, 'restore_skip_low_margin': 0,
                    'restore_skip_unsafe': 0, 'restore_evaluated': 0}

    st.subheader(f"📅 今日 ({activity.get('date','')})")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🚫 拒改价", len(activity.get('rejects', [])))
    c2.metric("🛑 关广告", len(activity.get('ad_offs', [])))
    c3.metric("✅ 恢复广告", activity.get('restore_ok', 0),
              delta=f"-{activity.get('restore_failed', 0)} 失败" if activity.get('restore_failed') else None)
    c4.metric("⚠️ 跳过(不安全)", activity.get('restore_skip_unsafe', 0))

    if activity.get('rejects'):
        with st.expander(f"🚫 被拒改价 SKU ({len(activity['rejects'])})"):
            st.code('\n'.join(activity['rejects']))
    if activity.get('ad_offs'):
        with st.expander(f"🛑 触底关广告 SKU ({len(activity['ad_offs'])})"):
            st.code('\n'.join(activity['ad_offs']))

    st.markdown("---")

    # ─── 最新 smart_bid 报告 ────────────────────────
    st.subheader("🎯 最新 Smart Bid 调整")
    sb_path = _latest_json('batch_smart_bid')
    sb = _read_json(sb_path)
    if sb:
        s = sb.get('summary', {})
        ar = sb.get('apply_result', {})
        st.caption(f"报告: `{sb_path.name}` · 时间 {sb.get('timestamp', '')[:19].replace('T',' ')} · "
                   f"模式 {'WRITE' if sb.get('apply') else 'DRY-RUN'}")
        bd = s.get('by_decision', {})
        bt = s.get('by_tier', {})
        cc1, cc2, cc3, cc4, cc5 = st.columns(5)
        cc1.metric("总计", s.get('total', 0))
        cc2.metric("调整", bd.get('adjust', 0), delta=f"-{ar.get('applied_failed', 0)} 失败" if ar.get('applied_failed') else None)
        cc3.metric("无变化", bd.get('no_change', 0))
        cc4.metric("跳过", bd.get('skip_unsafe', 0))
        cc5.metric("分级", f"H{bt.get('high', 0)}/M{bt.get('mid', 0)}/L{bt.get('low', 0)}")

        plan = sb.get('plan', [])
        adj_only = [p for p in plan if p.get('decision') == 'adjust']
        if adj_only:
            with st.expander(f"📊 调整明细 ({len(adj_only)})"):
                import pandas as pd
                df = pd.DataFrame(adj_only)[['sku', 'tier', 'current_bid', 'new_bid',
                                              'impressions', 'ctr', 'sold_qty', 'reason']]
                st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("暂无 Smart Bid 报告 — 周二 10:00 自动运行")

    st.markdown("---")

    # ─── 最新 bid_rollback 报告 ─────────────────────
    st.subheader("↩️ 最新 Bid 回溯")
    rb_path = _latest_json('bid_rollback_audit')
    rb = _read_json(rb_path)
    if rb:
        decs = rb.get('decisions', [])
        n_rb = sum(1 for d in decs if d['decision'] == 'rollback')
        n_keep = sum(1 for d in decs if d['decision'] == 'keep')
        n_nd = sum(1 for d in decs if d['decision'] == 'no_data')
        st.caption(f"报告: `{rb_path.name}` · 基线 {rb.get('baseline_file', 'N/A')}")
        rc1, rc2, rc3, rc4 = st.columns(4)
        rc1.metric("评估总数", len(decs))
        rc2.metric("回退", n_rb)
        rc3.metric("保留(有效)", n_keep)
        rc4.metric("数据不足", n_nd)
        rb_only = [d for d in decs if d['decision'] == 'rollback']
        if rb_only:
            with st.expander(f"↩️ 回退明细 ({len(rb_only)})"):
                import pandas as pd
                df = pd.DataFrame(rb_only)[['sku', 'current_bid', 'old_bid_target',
                                             'old_ctr', 'new_ctr', 'old_sold_qty',
                                             'new_sold_qty', 'reason']]
                st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("暂无 Bid 回溯报告 — 周二 11:00 自动运行")

    st.markdown("---")

    # ─── 广告黑名单管理 ───────────────────────────
    st.subheader("📛 广告黑名单")
    try:
        from src.services import ad_blacklist
        bl = ad_blacklist.list_all()
    except Exception as exc:
        st.error(f"加载黑名单失败: {exc}")
        bl = {}

    bc1, bc2 = st.columns(2)
    bc1.metric("黑名单 SKU 数", len([
        s for s, e in bl.items()
        if e.get('reason') in ('manual', 'auto_off_streak')
    ]))
    bc2.metric("追踪中 (off_count<3)", len([
        s for s, e in bl.items()
        if e.get('reason') == 'tracking'
    ]))

    if bl:
        import pandas as pd
        rows = [{'sku': s, **e} for s, e in bl.items()]
        df = pd.DataFrame(rows)
        cols = [c for c in ['sku', 'reason', 'off_count', 'added_at',
                            'last_off_at', 'note'] if c in df.columns]
        st.dataframe(df[cols].sort_values('off_count', ascending=False),
                     use_container_width=True, hide_index=True)

    with st.expander("➕ 手动管理黑名单"):
        with st.form("blacklist_add"):
            new_sku = st.text_input("加入 SKU")
            note = st.text_input("备注", value="manual")
            if st.form_submit_button("加入黑名单") and new_sku:
                from src.services import ad_blacklist as _bl
                _bl.add_manual(new_sku.strip(), note=note)
                st.success(f"✅ {new_sku} 已加入")
                st.rerun()
        with st.form("blacklist_remove"):
            rm_sku = st.text_input("移除 SKU")
            if st.form_submit_button("移除") and rm_sku:
                from src.services import ad_blacklist as _bl
                if _bl.remove(rm_sku.strip()):
                    st.success(f"✅ {rm_sku} 已移除")
                    st.rerun()
                else:
                    st.warning("未找到")

    st.markdown("---")

    # ─── 价格 / Bid 历史趋势 (P9) ──────────────────
    st.subheader("📈 价格 / Bid 历史趋势 (近 30 天)")
    try:
        from src.services.pricing_history import get_history
        sku_q = st.text_input("查询 SKU", key="ph_sku")
        days_q = st.slider("天数", 7, 90, 30, key="ph_days")
        if sku_q:
            hist = get_history(sku_q.strip(), days=days_q)
            if not hist:
                st.info(f"{sku_q} 暂无历史 — 智能调价触及该 SKU 当日才会落库")
            else:
                import pandas as pd
                df = pd.DataFrame(hist)
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
                price_cols = [c for c in ['live_price', 'total_cost'] if c in df.columns]
                bid_cols = [c for c in ['current_bid_pct', 'max_safe_ad_rate'] if c in df.columns]
                if price_cols:
                    st.markdown("**价格 / 成本**")
                    st.line_chart(df[price_cols])
                if bid_cols:
                    st.markdown("**Bid % / 现价支持的最大广告率**")
                    st.line_chart(df[bid_cols])
                with st.expander("原始记录"):
                    st.dataframe(df.reset_index(), use_container_width=True, hide_index=True)
    except Exception as exc:
        st.warning(f"加载历史失败: {exc}")

    st.markdown("---")

    # ─── PLA Campaign 容量 (P10) ───────────────────
    st.subheader("📊 PLA Campaign 容量")
    cc_path = _latest_json('campaign_capacity')
    cc = _read_json(cc_path)
    if cc:
        st.caption(f"报告: `{cc_path.name}` · {cc.get('timestamp','')[:19].replace('T',' ')} · "
                   f"全局填充 {cc.get('global_fill_pct',0)*100:.1f}%")
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Campaign 总数", len(cc.get('campaigns', [])))
        cc2.metric("饱和 (≥9000)", len(cc.get('saturated', [])),
                   delta_color='inverse' if cc.get('saturated') else 'off')
        cc3.metric("低填充可迁入", len(cc.get('available', [])))
        if cc.get('campaigns'):
            import pandas as pd
            df = pd.DataFrame(cc['campaigns'])
            st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("暂无 Campaign 容量报告 — 跑 `python scripts/campaign_capacity_audit.py`")

    st.markdown("---")

    # ─── A/B 实验 (P12) ─────────────────────────────
    st.subheader("🧪 Bid A/B 实验")
    try:
        from src.services import bid_experiment as bx
        running = bx.list_experiments(status='running')
        completed = bx.list_experiments(status='completed')
        ec1, ec2 = st.columns(2)
        ec1.metric("进行中", len(running))
        ec2.metric("已完成", len(completed))
        if running:
            import pandas as pd
            df = pd.DataFrame(running)[['experiment_id', 'name', 'control_bid',
                                          'variant_bid', 'created_at', 'end_at']]
            st.markdown("**进行中**")
            st.dataframe(df, use_container_width=True, hide_index=True)
        if completed:
            with st.expander(f"已完成 {len(completed)} 个 — 结果"):
                import pandas as pd, json as _json
                rows = []
                for e in completed:
                    rj = _json.loads(e.get('result_json') or '{}')
                    rows.append({
                        'experiment_id': e['experiment_id'], 'name': e['name'],
                        'control_avg_ctr': rj.get('control_avg_ctr'),
                        'variant_avg_ctr': rj.get('variant_avg_ctr'),
                        'lift_ctr_pct': rj.get('lift_ctr_pct'),
                        'is_significant': rj.get('is_significant'),
                        'recommendation': rj.get('recommendation'),
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        if not running and not completed:
            st.info("暂无实验 — 用 `bid_experiment.create_experiment(...)` 创建")
    except Exception as exc:
        st.warning(f"加载实验失败: {exc}")
