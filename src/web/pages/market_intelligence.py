"""
Market Intelligence 页面

Streamlit 页面：市场智能分析与决策支持

主要功能:
1. 自动发掘爆品 - 分析已采集产品
2. 大建收藏分析 - 基于收藏产品发现机会
3. 趋势发掘 - 主动发现未采集的市场机会
4. Item Specifics 建议 - 推荐高需求属性
5. 关键词分析 - 单个关键词深入分析
6. 智能定价 - 计算最优售价
"""
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import sys
from pathlib import Path

# 添加项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from src.utils.store_profile import get_store_profile

_SERVER_BASE_URL = get_store_profile().server_base_url


# ----- 缩略图辅助 -----
# GigaB2B / 大建云仓图片 URL 带有签名参数 (x-cc/x-cu/x-ct/x-cs)，
# 必须保留；这里只在多个常见字段中拽出第一张可用图。
DEF_THUMB_KEYS = (
    "image_url", "imageUrl", "picUrl", "pic", "mainImage", "mainPic",
    "productImage", "productImg", "firstImg", "thumbnail", "cover",
    "coverImg", "coverUrl", "skuPic",
)
DEF_THUMB_LIST_KEYS = ("images", "imageUrls", "picUrls", "productImages")


def _extract_thumb(record: dict) -> str:
    """尽量宽容地从 dict 中拿出第一张产品缩略图 URL。"""
    if not isinstance(record, dict):
        return ""
    for k in DEF_THUMB_KEYS:
        v = record.get(k)
        if isinstance(v, str) and v.strip().startswith("http"):
            return v.strip()
    for k in DEF_THUMB_LIST_KEYS:
        v = record.get(k)
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str) and first.strip().startswith("http"):
                return first.strip()
            if isinstance(first, dict):
                inner = _extract_thumb(first)
                if inner:
                    return inner
    return ""


def _render_thumb(record: dict, *, width: int = 140, caption: str = "") -> bool:
    """Streamlit 缩略图渲染，拿不到或加载失败时返回 False。"""
    url = _extract_thumb(record)
    if not url:
        st.caption("_(无可用缩略图)_")
        return False
    try:
        st.image(url, width=width, caption=caption or None)
        return True
    except Exception:
        st.caption("_(缩略图加载失败)_")
        return False


# ----- SKU 屏蔽名单持久化 (F1.1: 支持 TTL) -----
_BLACKLIST_PATH = PROJECT_ROOT / "reports" / "mi_blacklist.json"


def _load_blacklist_entries() -> dict:
    """读取并自动剥离过期项；返回 {sku: {added_at, expires_at, reason}}.

    向后兼容：旧版 list 形态视作永久条目。
    """
    import json
    from datetime import datetime as _dt
    entries: dict = {}
    if not _BLACKLIST_PATH.exists():
        return entries
    try:
        data = json.loads(_BLACKLIST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return entries
    if isinstance(data, list):
        for sku in data:
            if sku:
                entries[str(sku)] = {"added_at": None, "expires_at": None, "reason": None}
        return entries
    if isinstance(data, dict):
        now = _dt.now()
        for sku, meta in data.items():
            if not sku:
                continue
            meta = meta if isinstance(meta, dict) else {}
            exp = meta.get("expires_at")
            if exp:
                try:
                    if _dt.fromisoformat(exp) < now:
                        continue  # 过期，跳过
                except Exception:
                    pass
            entries[str(sku)] = {
                "added_at": meta.get("added_at"),
                "expires_at": exp,
                "reason": meta.get("reason"),
            }
    return entries


def _save_blacklist_entries(entries: dict) -> None:
    import json
    try:
        _BLACKLIST_PATH.parent.mkdir(exist_ok=True)
        _BLACKLIST_PATH.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_blacklist() -> set:
    """返回当前生效的屏蔽 SKU 集合（已自动过滤过期项）。"""
    return set(_load_blacklist_entries().keys())


def _save_blacklist(skus: set) -> None:
    """覆盖式写入：保留已知 SKU 的元数据，新增项默认为永久。"""
    existing = _load_blacklist_entries()
    from datetime import datetime as _dt
    new_entries: dict = {}
    now_iso = _dt.now().isoformat()
    for sku in skus:
        sku = str(sku)
        if sku in existing:
            new_entries[sku] = existing[sku]
        else:
            new_entries[sku] = {"added_at": now_iso, "expires_at": None, "reason": None}
    _save_blacklist_entries(new_entries)


def _add_blacklist_entry(sku: str, ttl_days: int | None = None, reason: str | None = None) -> None:
    """追加单个屏蔽项；ttl_days=None 表示永久。"""
    from datetime import datetime as _dt, timedelta as _td
    entries = _load_blacklist_entries()
    now = _dt.now()
    expires_at = (now + _td(days=ttl_days)).isoformat() if ttl_days else None
    entries[str(sku)] = {
        "added_at": now.isoformat(),
        "expires_at": expires_at,
        "reason": reason,
    }
    _save_blacklist_entries(entries)


def render_market_intelligence():
    """渲染市场智能分析页面"""
    st.title("🎯 Market Intelligence")
    st.caption("智能选品与市场分析决策引擎")
    
    # 侧边栏配置
    with st.sidebar:
        st.subheader("⚙️ 分析配置")
        min_margin = st.slider("最低利润率", 0.05, 0.30, 0.15, 0.05, key="sidebar_margin")
        max_results = st.slider("最大推荐数量", 5, 50, 20, 5, key="sidebar_results")

        # F2-pivot — Marketplace Insights 状态横幅
        # eBay 已拒绝我们的 MI API 申请，所有 STR 数值均为 Browse 估算
        with st.expander("📡 数据源状态", expanded=False):
            try:
                from src.plugins.terapeak_research.research_client import TerapeakClient
                _probe = TerapeakClient().check_marketplace_insights_access()
                if _probe.get("available"):
                    st.success("✅ Marketplace Insights 可用 (sold-data 真实)")
                else:
                    _reason = _probe.get("reason", "unknown")
                    _label = {
                        "permission_denied": "🚫 权限被拒 — eBay 已拒绝 MI API 申请",
                        "endpoint_not_found": "❌ 端点不可达",
                        "transport_error": "⚠️ 网络异常",
                        "unavailable": "⚠️ 暂不可用",
                    }.get(_reason, f"⚠️ {_reason}")
                    st.warning(_label)
                    st.caption(
                        "所有市场数据来自 Browse API（活跃在售）。"
                        "**STR / 需求分均为估算**，不是真实成交率。"
                    )
            except Exception as _e:
                st.caption(f"状态探测失败: {_e}")

        # F6 — 竞争监控真实表现覆盖 (Analytics + Fulfillment cache)
        with st.expander("📈 已刊 SKU 实测覆盖 (来自竞争监控)", expanded=False):
            try:
                from src.plugins.terapeak_research.performance_bridge import (
                    load_seller_performance_index,
                )
                _perf_idx = load_seller_performance_index(max_age_hours=24)
                _with_imp = sum(1 for v in _perf_idx.values() if v.get('impressions', 0) > 0)
                _with_str = sum(1 for v in _perf_idx.values() if v.get('str_pct') is not None)
                _with_sales = sum(1 for v in _perf_idx.values() if v.get('has_sales'))
                pc1, pc2, pc3 = st.columns(3)
                pc1.metric("有展示", _with_imp)
                pc2.metric("实测STR", _with_str)
                pc3.metric("有销售", _with_sales)
                if _perf_idx:
                    _ts = next(iter(_perf_idx.values())).get('fetched_at', '')
                    st.caption(
                        f"性能缓存: {_ts[:19] if _ts else '-'} · "
                        "实测 STR ≥ 50 展示才计算；其余 SKU 走 Browse 估算"
                    )
                else:
                    st.caption(
                        "⚠️ 暂无性能缓存。打开「📈 竞争监控 → 🔄 刷新性能数据」生成后，"
                        "MI 评分会自动用真实 STR 替换估算值（权重 18→30）。"
                    )
            except Exception as _perf_err:
                st.caption(f"性能桥接不可用: {_perf_err}")

        # E2 — 缓存与性能诊断 (B1 TTLCache 可视性)
        with st.expander("🧠 缓存与性能诊断", expanded=False):
            try:
                from src.plugins.terapeak_research._cache import get_response_cache
                _cache = get_response_cache()
                _stats = _cache.stats()
                cs1, cs2 = st.columns(2)
                cs1.metric("缓存命中", _stats.get("hits", 0))
                cs2.metric("未命中", _stats.get("misses", 0))
                st.metric("命中率", f"{_stats.get('hit_rate', 0)*100:.1f}%")
                st.caption(
                    f"条目 {_stats.get('size', 0)}/{_stats.get('maxsize', 0)} · TTL {_stats.get('ttl', 0)}s"
                )
                if st.button("🗑️ 清空缓存", key="clear_cache_btn"):
                    _cache.clear()
                    st.success("已清空")
                    st.rerun()
                import os as _os
                st.caption(
                    f"并发 worker: TERAPEAK_CONCURRENCY={_os.getenv('TERAPEAK_CONCURRENCY', '6')}"
                )
            except Exception as _diag_err:
                st.caption(f"诊断不可用: {_diag_err}")

        # E3 — 屏蔽名单管理 (F1.1: TTL 支持)
        with st.expander("🚫 SKU 屏蔽名单", expanded=False):
            _bl_entries = _load_blacklist_entries()
            _bl_now = sorted(_bl_entries.keys())
            st.caption(f"当前共 {len(_bl_now)} 个生效中的屏蔽 SKU（持久化于 `reports/mi_blacklist.json`，过期项已自动剥离）")

            # F14 — 按 reason 聚合统计
            if _bl_entries:
                _reason_counts: dict = {}
                for _sku, _meta in _bl_entries.items():
                    _r = _meta.get("reason") or "(未标注)"
                    _reason_counts[_r] = _reason_counts.get(_r, 0) + 1
                _summary = " · ".join(
                    f"{r}:{c}" for r, c in sorted(_reason_counts.items(), key=lambda x: -x[1])
                )
                st.caption(f"📋 按原因: {_summary}")
                # audit_blocked 占比过高 → 提示
                _ab = _reason_counts.get("audit_blocked", 0)
                if _ab >= 5 and _ab / max(len(_bl_entries), 1) >= 0.5:
                    st.warning(
                        f"⚠️ 已有 {_ab} 个 SKU 因审计阻塞被屏蔽（占 {_ab/len(_bl_entries):.0%}），"
                        "建议优先修复 `audit_fix_ready_drafts` 失败规则。"
                    )
            if _bl_now:
                from datetime import datetime as _dt2
                _now = _dt2.now()
                _rows = []
                for _sku in _bl_now:
                    _meta = _bl_entries[_sku]
                    _exp = _meta.get("expires_at")
                    if not _exp:
                        _remaining = "永久"
                    else:
                        try:
                            _delta_days = (_dt2.fromisoformat(_exp) - _now).days
                            _remaining = f"剩余 {_delta_days}d" if _delta_days >= 0 else "已过期"
                        except Exception:
                            _remaining = "?"
                    _rows.append({"SKU": _sku, "状态": _remaining, "原因": _meta.get("reason") or ""})
                st.dataframe(_rows, use_container_width=True, hide_index=True)
                _to_remove = st.multiselect("解除屏蔽", _bl_now, key="bl_remove_select")
                if st.button("✅ 解除选中", key="bl_remove_btn") and _to_remove:
                    _remaining_entries = {k: v for k, v in _bl_entries.items() if k not in set(_to_remove)}
                    _save_blacklist_entries(_remaining_entries)
                    st.success(f"已解除 {len(_to_remove)} 个 SKU")
                    st.rerun()
            _new_skus = st.text_area("手动追加 (每行一个 SKU)", key="bl_add_text", height=80)
            _ttl_choice = st.selectbox("追加项时长", ["永久", "7天", "30天"], key="bl_add_ttl")
            if st.button("➕ 追加屏蔽", key="bl_add_btn") and _new_skus.strip():
                _added = {s.strip() for s in _new_skus.splitlines() if s.strip()}
                _ttl_days = {"永久": None, "7天": 7, "30天": 30}[_ttl_choice]
                for _s in _added:
                    _add_blacklist_entry(_s, ttl_days=_ttl_days)
                st.success(f"已追加 {len(_added)} 个 SKU ({_ttl_choice})")
                st.rerun()
    
    # 主标签页 - 新增更多功能
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "🔥 自动发掘爆品",
        "⭐ 大建收藏分析",
        "📈 趋势发掘",
        "🔍 关键词分析", 
        "📋 Item Specifics 建议",
        "💰 智能定价"
    ])
    
    # ========== Tab 1: 自动爆品发掘 (新增) ==========
    with tab1:
        st.subheader("🔥 自动发掘爆品 - 主动发现高潜力产品")
        
        # 解释说明
        with st.expander("ℹ️ 什么是「库存产品」？点击了解", expanded=False):
            st.markdown("""
            ### 📦 库存产品 = 您已采集的大建云仓产品
            
            **工作流程：**
            1. 使用浏览器扩展在大建云仓网站采集产品 → 产品进入数据库（状态：PENDING）
            2. 本功能分析这些**待刊登**的产品，对比 eBay 市场数据
            3. 找出利润空间大、竞争适中的产品 → 建议您优先刊登
            
            **发掘的意义：**
            - 🎯 帮您从众多采集的产品中，筛选出**最值得刊登**的
            - 💰 避免刊登利润太低或竞争过于激烈的产品
            - 📊 提供市场均价参考，帮助合理定价
            
            **如何采集产品？**
            1. 安装浏览器扩展 (extension/ 文件夹)
            2. 访问大建云仓产品页面
            3. 点击扩展按钮采集产品
            """)
        
        st.markdown("""
        系统将分析您**已采集但尚未刊登**的大建产品，与 eBay 实时市场数据对比，
        找出**利润空间大且好卖**的产品。
        """)
        
        col1, col2, col3 = st.columns(3)
        with col1:
            discover_margin = st.slider("最低利润率要求", 0.10, 0.40, 0.20, 0.05, 
                                       help="只显示利润率高于此值的产品", key="discover_margin")
        with col2:
            discover_limit = st.slider("显示数量", 5, 50, 15, 5, key="discover_limit")
        with col3:
            st.write("")  # 占位
            discover_btn = st.button("🚀 开始发掘爆品", type="primary", use_container_width=True, key="discover_btn")
        
        if discover_btn:
            with st.spinner("🔍 正在分析库存产品与市场数据..."):
                try:
                    from src.plugins.terapeak_research.intelligence_service import IntelligenceService
                    from src.utils.mi_opportunity_flow import auto_prepare_mi_opportunity_drafts
                    from daily_tasks import analyze_collected_products

                    intel = IntelligenceService()
                    
                    opportunities = intel.auto_discover_opportunities(
                        min_margin=discover_margin,
                        max_results=discover_limit
                    )
                    
                    if opportunities:
                        # E3 — 屏蔽名单过滤
                        _blacklist = _load_blacklist()
                        if _blacklist:
                            before = len(opportunities)
                            opportunities = [o for o in opportunities if o.get("sku") not in _blacklist]
                            removed = before - len(opportunities)
                            if removed:
                                st.caption(f"🚫 已根据屏蔽名单过滤 {removed} 个 SKU（共 {len(_blacklist)} 个屏蔽）")

                    auto_prepare_result = auto_prepare_mi_opportunity_drafts(
                        opportunities,
                        analyze_collected_products=analyze_collected_products,
                    ) if opportunities else {"success": 0, "prepared_skus": []}

                    if opportunities:
                        _auto_prepared_count = len(auto_prepare_result.get("prepared_skus") or [])
                        if _auto_prepared_count:
                            st.info(
                                f"🧠 已自动起草 {_auto_prepared_count} 个 MI 机会草稿；"
                                "现在会出现在 Dashboard 的 READY 区，带 MI 标记，等待审核后刊登。"
                            )
                        st.success(f"✅ 发现 {len(opportunities)} 个高潜力产品！")
                        
                        # 统计卡片
                        high_score = [o for o in opportunities if o['opportunity_score'] >= 70]
                        avg_margin = sum(o['margin_rate'] for o in opportunities) / len(opportunities)
                        
                        col1, col2, col3, col4 = st.columns(4)
                        col1.metric("发现机会", len(opportunities))
                        col2.metric("强烈推荐", len(high_score), "🔥")
                        col3.metric("平均利润率", f"{avg_margin:.1f}%")
                        col4.metric("潜在总利润", f"${sum(o['potential_profit'] for o in opportunities):,.0f}")

                        # F11 — KPI 横幅（基于历史快照对比）
                        try:
                            from src.plugins.terapeak_research.history import (
                                load_mi_snapshots, summarize_recent_history,
                            )
                            _kpi_history = load_mi_snapshots(window_days=14, project_root=PROJECT_ROOT)
                            _kpi = summarize_recent_history(_kpi_history, opportunities)
                            _bl_active = _load_blacklist()
                            k1, k2, k3, k4 = st.columns(4)
                            k1.metric(
                                "📊 14d 平均机会数",
                                f"{_kpi['avg_recent_count']}",
                                help=f"基于最近 {_kpi['snapshots_analyzed']} 份快照",
                            )
                            _delta_label = (
                                f"+{_kpi['delta_vs_avg']}" if _kpi['delta_vs_avg'] > 0
                                else f"{_kpi['delta_vs_avg']}"
                            )
                            k2.metric(
                                "🆚 本次 vs 平均",
                                f"{_kpi['current_count']}",
                                _delta_label if _kpi['snapshots_analyzed'] > 0 else None,
                            )
                            k3.metric(
                                "📈 实测 STR 覆盖率",
                                f"{_kpi['real_str_coverage_pct']}%",
                                help="本次结果中带 real_str 的 SKU 占比",
                            )
                            k4.metric(
                                "🚫 屏蔽中 SKU",
                                f"{len(_bl_active)}",
                                help="生效中的 mi_blacklist 条目（已剥离过期）",
                            )
                            # F23 — 30d 长周期透视（短/长窗口漂移）
                            try:
                                from src.plugins.terapeak_research.history import summarize_long_window
                                _kpi_long_history = load_mi_snapshots(window_days=60, project_root=PROJECT_ROOT)
                                _long = summarize_long_window(
                                    _kpi_long_history, opportunities,
                                    short_days=14, long_days=30,
                                )
                                if _long["snapshots_long"] >= 3:
                                    _label_map = {
                                        "rising": "📈 回暖", "falling": "📉 衰退",
                                        "stable": "➖ 平稳", "insufficient": "❔ 数据不足",
                                    }
                                    _drift = _long["drift"]
                                    _drift_str = (f"+{_drift}" if _drift > 0 else f"{_drift}")
                                    st.caption(
                                        f"🔭 长周期透视：14d 均值 {_long['short_avg_count']} · "
                                        f"30d 均值 {_long['long_avg_count']} · "
                                        f"漂移 {_drift_str} · "
                                        f"长窗口平均分 {_long['long_avg_score']} · "
                                        f"趋势 **{_label_map.get(_long['trend_label'], _long['trend_label'])}**"
                                        f"（基于 {_long['snapshots_long']} 份 30d 快照）"
                                    )
                            except Exception as _long_err:
                                st.caption(f"⚠️ 长周期透视不可用: {_long_err}")
                            # F15 — 14d KPI 折线图（折叠在 expander 中，避免占用首屏）
                            if _kpi['snapshots_analyzed'] >= 2:
                                with st.expander("📈 14d KPI 历史走势", expanded=False):
                                    try:
                                        from src.plugins.terapeak_research.history import build_kpi_timeseries
                                        _ts = build_kpi_timeseries(_kpi_history)
                                        if _ts:
                                            import pandas as _pd
                                            _df = _pd.DataFrame(_ts).set_index("date")
                                            st.line_chart(
                                                _df[["opportunity_count", "avg_score"]],
                                                height=200,
                                            )
                                            st.caption("📊 实测 STR 覆盖率%")
                                            st.line_chart(
                                                _df[["real_str_coverage_pct"]],
                                                height=160,
                                            )
                                            # F30 — 按品类切片趋势
                                            try:
                                                from src.plugins.terapeak_research.history import (
                                                    list_known_categories, build_category_timeseries,
                                                )
                                                _cats = list_known_categories(_kpi_history)
                                                if _cats:
                                                    _picked_cat = st.selectbox(
                                                        "🗂️ 按品类切片趋势",
                                                        options=["（不切片）"] + _cats,
                                                        key="t1_cat_slice",
                                                    )
                                                    if _picked_cat and _picked_cat != "（不切片）":
                                                        _cat_ts = build_category_timeseries(
                                                            _kpi_history, _picked_cat,
                                                        )
                                                        if _cat_ts:
                                                            _cdf = _pd.DataFrame(_cat_ts).set_index("date")
                                                            st.line_chart(
                                                                _cdf[["count", "avg_score"]],
                                                                height=200,
                                                            )
                                                            st.caption(
                                                                f"💲 `{_picked_cat}` 中位价 / 潜在利润"
                                                            )
                                                            st.line_chart(
                                                                _cdf[["median_price", "total_potential_profit"]],
                                                                height=160,
                                                            )
                                                        else:
                                                            st.caption(
                                                                f"⚠️ `{_picked_cat}` 在历史快照中没有记录（旧快照可能未保存 by_category）"
                                                            )
                                            except Exception as _cs_err:
                                                st.caption(f"⚠️ 品类切片不可用: {_cs_err}")
                                    except Exception as _ts_err:
                                        st.caption(f"⚠️ 走势图不可用: {_ts_err}")
                        except Exception as _kpi_err:
                            st.caption(f"⚠️ KPI 横幅不可用: {_kpi_err}")

                        # F19 — 品类聚合透视（识别"哪个大类正在升温"）
                        try:
                            from src.plugins.terapeak_research.aggregation import (
                                aggregate_by_category, derive_product_category,
                            )
                            _cat_rows = aggregate_by_category(opportunities)
                            if _cat_rows:
                                with st.expander(f"🗂️ 品类视图（{len(_cat_rows)} 个大类）", expanded=False):
                                    import pandas as _pd
                                    _cat_df = _pd.DataFrame(_cat_rows)
                                    # 友好列序
                                    _col_order = ["category", "count", "avg_score", "median_score",
                                                  "median_price", "avg_margin_rate",
                                                  "total_potential_profit", "real_str_count",
                                                  "top_sku", "top_score"]
                                    _cat_df = _cat_df[[c for c in _col_order if c in _cat_df.columns]]
                                    st.dataframe(_cat_df, use_container_width=True, hide_index=True)
                                    st.caption("💡 `count` 越高表示该大类机会越多；"
                                               "`avg_score` 反映该大类整体潜力；"
                                               "`real_str_count` 是该大类内含真实 STR 数据的 SKU 数。")
                                    # F26 — 品类钻取联动：勾选后过滤下方机会列表
                                    _cat_options = [r["category"] for r in _cat_rows]
                                    st.multiselect(
                                        "🔗 钻取：仅显示选中大类（清空则显示全部）",
                                        options=_cat_options,
                                        default=[],
                                        key="t1_cat_drill",
                                        help="选中的大类会同步过滤下方筛选与排序结果",
                                    )
                        except Exception as _agg_err:
                            st.caption(f"⚠️ 品类视图不可用: {_agg_err}")

                        # F29 — MI 日报历史归档浏览器
                        try:
                            _digest_dir = PROJECT_ROOT / "reports"
                            _digests = sorted(
                                _digest_dir.glob("mi_digest_*.html"),
                                key=lambda p: p.stat().st_mtime,
                                reverse=True,
                            ) if _digest_dir.exists() else []
                            if _digests:
                                with st.expander(f"📦 历史日报归档（{len(_digests)} 份，最近 3 天）", expanded=False):
                                    _options = {p.name: p for p in _digests}
                                    _picked = st.selectbox(
                                        "选择一份归档查看",
                                        options=list(_options.keys()),
                                        key="t1_digest_pick",
                                    )
                                    if _picked:
                                        _path = _options[_picked]
                                        try:
                                            from datetime import datetime as _dt
                                            _html = _path.read_text(encoding="utf-8")
                                            st.caption(
                                                f"🗂 文件: `{_picked}` · 大小: {_path.stat().st_size:,} B · "
                                                f"修改时间: {_dt.fromtimestamp(_path.stat().st_mtime):%Y-%m-%d %H:%M}"
                                            )
                                            st.components.v1.html(_html, height=600, scrolling=True)
                                            st.download_button(
                                                "⬇️ 下载该归档",
                                                data=_html.encode("utf-8"),
                                                file_name=_picked,
                                                mime="text/html",
                                                key=f"t1_digest_dl_{_picked}",
                                            )
                                        except Exception as _read_err:
                                            st.warning(f"读取失败: {_read_err}")
                        except Exception as _arch_err:
                            st.caption(f"⚠️ 历史日报浏览器不可用: {_arch_err}")

                        st.divider()

                        # E1 — 筛选 / 排序 / 视图切换
                        st.markdown("### 🎛️ 筛选与排序")
                        f1, f2, f3, f4 = st.columns([1.2, 1, 1, 1])
                        with f1:
                            sort_key = st.selectbox(
                                "排序",
                                ["机会分数 ↓", "潜在利润 ↓", "利润率 ↓", "市场均价 ↓", "大建成本 ↑"],
                                key="t1_sort",
                            )
                        with f2:
                            status_options = sorted({o.get("status", "") or "未知" for o in opportunities})
                            default_statuses = [s for s in status_options if s != "PUBLISHED"] or status_options
                            status_filter = st.multiselect(
                                "状态过滤", status_options, default=default_statuses, key="t1_status"
                            )
                        with f3:
                            min_profit = st.number_input(
                                "最低利润 ($)", min_value=0.0, value=0.0, step=10.0, key="t1_minprofit"
                            )
                        with f4:
                            view_mode = st.radio("视图", ["卡片", "表格"], horizontal=True, key="t1_view")

                        sort_map = {
                            "机会分数 ↓": ("opportunity_score", True),
                            "潜在利润 ↓": ("potential_profit", True),
                            "利润率 ↓": ("margin_rate", True),
                            "市场均价 ↓": ("market_avg_price", True),
                            "大建成本 ↑": ("dajian_cost", False),
                        }
                        sk, reverse = sort_map[sort_key]
                        # F26 — 品类钻取过滤
                        _drill_cats = st.session_state.get("t1_cat_drill", []) or []
                        if _drill_cats:
                            try:
                                from src.plugins.terapeak_research.aggregation import derive_product_category as _dpc
                                _drill_set = set(_drill_cats)
                                opps_for_filter = [
                                    o for o in opportunities
                                    if _dpc(o.get("title")) in _drill_set
                                ]
                            except Exception:
                                opps_for_filter = opportunities
                        else:
                            opps_for_filter = opportunities
                        filtered = [
                            o for o in opps_for_filter
                            if (o.get("status", "") or "未知") in status_filter
                            and o.get("potential_profit", 0) >= min_profit
                        ]
                        filtered.sort(key=lambda x: x.get(sk, 0), reverse=reverse)

                        st.caption(f"显示 {len(filtered)} / {len(opportunities)} 条"
                                   + (f" · 已按品类钻取 {len(_drill_cats)} 类" if _drill_cats else ""))

                        if view_mode == "表格":
                            df_view = pd.DataFrame([
                                {
                                    "SKU": o["sku"],
                                    "标题": (o["title"][:60] + "…") if len(o.get("title", "")) > 60 else o.get("title", ""),
                                    "状态": o.get("status", ""),
                                    "机会分": o["opportunity_score"],
                                    "需求(估)": o.get("demand_signal_score", 0),
                                    "实测STR%": (
                                        f"{o['seller_str_pct']:.1f}"
                                        if o.get("seller_str_pct") is not None else "-"
                                    ),
                                    "已售": o.get("seller_sold", 0),
                                    "成本$": o["dajian_cost"],
                                    "建议价$": o["suggested_price"],
                                    "市场均价$": o["market_avg_price"],
                                    "利润$": o["potential_profit"],
                                    "利润率%": o["margin_rate"],
                                    "竞争": o["competition"],
                                    "图": _extract_thumb(o),
                                    "大建链接": o.get("url", ""),
                                }
                                for o in filtered
                            ])
                            try:
                                st.dataframe(
                                    df_view,
                                    use_container_width=True,
                                    column_config={
                                        "图": st.column_config.ImageColumn("缩略图", width="small"),
                                        "大建链接": st.column_config.LinkColumn("链接", width="small"),
                                    },
                                )
                            except Exception:
                                st.dataframe(df_view, use_container_width=True)

                        # 卡片视图（兼容默认行为，并提供屏蔽按钮）
                        opportunities = filtered
                        if view_mode == "卡片":
                            # 产品列表
                            st.markdown("### 📋 推荐刊登产品")

                            # F7 — 一次性加载历史快照供卡片趋势徽章使用
                            try:
                                from src.plugins.terapeak_research.history import (
                                    load_mi_snapshots,
                                    compute_score_trend,
                                    compute_field_trend,
                                    render_sparkline,
                                )
                                _mi_history = load_mi_snapshots(window_days=14, project_root=PROJECT_ROOT)
                            except Exception:
                                _mi_history = []
                                compute_score_trend = None  # type: ignore
                                compute_field_trend = None  # type: ignore
                                render_sparkline = None  # type: ignore

                            for i, opp in enumerate(opportunities, 1):
                                score = opp['opportunity_score']
                                score_emoji = "🔥" if score >= 80 else "✅" if score >= 65 else "⚠️"

                                with st.expander(
                                    f"{score_emoji} #{i} {opp['sku']} - 机会分数: {score} | 利润: ${opp['potential_profit']:.0f}"
                                ):
                                    thumb_col, col1, col2 = st.columns([1, 2, 1])

                                    with thumb_col:
                                        _render_thumb(opp, width=140)

                                    with col1:
                                        st.write(f"**产品:** {opp['title']}")
                                        st.write(f"**搜索关键词:** `{opp['search_keywords']}`")
                                        st.write(f"**状态:** {opp['status']}")
                                        if opp.get('url'):
                                            st.markdown(f"[🔗 查看大建链接]({opp['url']})")

                                    with col2:
                                        st.metric("大建成本", f"${opp['dajian_cost']:.2f}")
                                        st.metric("市场均价", f"${opp['market_avg_price']:.2f}")
                                        st.metric("建议售价", f"${opp['suggested_price']:.2f}")

                                    st.divider()

                                    col1, col2, col3, col4 = st.columns(4)
                                    col1.metric("利润空间", f"${opp['potential_profit']:.2f}")
                                    col2.metric("利润率", f"{opp['margin_rate']:.1f}%")
                                    col3.metric("竞争程度", opp['competition'].upper())
                                    with col4:
                                        _ttl_label = st.selectbox(
                                            "屏蔽时长",
                                            options=["永久", "7天", "30天"],
                                            key=f"bl_ttl_{opp['sku']}_{i}",
                                            label_visibility="collapsed",
                                        )
                                        if st.button("🚫 加屏蔽", key=f"bl_{opp['sku']}_{i}"):
                                            _ttl_map = {"永久": None, "7天": 7, "30天": 30}
                                            _add_blacklist_entry(opp['sku'], ttl_days=_ttl_map[_ttl_label])
                                            st.success(f"已将 {opp['sku']} 加入屏蔽名单 ({_ttl_label})")
                                            st.rerun()

                                    # F3 — 一键审计 + 发布闭环（仅对 READY 草稿启用）
                                    _opp_status = (opp.get("status") or "").upper()
                                    if _opp_status in ("READY", "READY_TO_PUBLISH"):
                                        if st.button(
                                            "🚀 一键审计 + 发布",
                                            key=f"ap_{opp['sku']}_{i}",
                                            help="调用 /api/mi/audit-and-publish/{sku}：先跑 audit_fix_ready_drafts，再发布到 eBay",
                                        ):
                                            import requests as _rq
                                            with st.spinner(f"审计 + 发布 {opp['sku']} 中（可能 30-60s）..."):
                                                try:
                                                    _r = _rq.post(
                                                        f"{_SERVER_BASE_URL}/api/mi/audit-and-publish/{opp['sku']}",
                                                        timeout=180,
                                                    )
                                                    _data = _r.json() if _r.headers.get("content-type", "").startswith("application/json") else {"raw": _r.text}
                                                except Exception as _e:
                                                    st.error(f"调用失败: {_e}")
                                                    _data = None
                                            if _data is not None:
                                                _status = (_data.get("status") or "").lower()
                                                if _status in ("success", "already_published"):
                                                    st.success(f"✅ {_status} · listing_id={_data.get('listing_id', '-')}")
                                                    if _data.get("audit"):
                                                        st.caption(
                                                            f"审计修正 {_data['audit'].get('changed', 0)} 项 · 未解决 {_data['audit'].get('unresolved', 0)} 项"
                                                        )
                                                elif _status == "audit_blocked":
                                                    st.error(f"❌ 审计未通过: {_data.get('message')}")
                                                    with st.expander("详情"):
                                                        st.json(_data.get("details") or _data)
                                                else:
                                                    st.error(f"❌ {_data.get('step', '?')}: {_data.get('message', _data)}")
                                                    with st.expander("详情"):
                                                        st.json(_data)
                                    else:
                                        st.caption(f"💡 当前状态 `{_opp_status or '未知'}` 不可一键发布；需先转为 READY。")

                                    # F6 — 实测 STR 徽章（来自竞争监控性能缓存，比 Browse 估算更可信）
                                    _real_str = opp.get('seller_str_pct')
                                    _seller_imp = opp.get('seller_impressions', 0)
                                    _seller_sold = opp.get('seller_sold', 0)
                                    if _real_str is not None:
                                        _rs_color = (
                                            "#16a085" if _real_str >= 5 else
                                            "#27ae60" if _real_str >= 2 else
                                            "#7f8c8d"
                                        )
                                        st.markdown(
                                            f"<div style='margin-top:8px'><span style='background:{_rs_color};color:white;"
                                            f"padding:3px 10px;border-radius:12px;font-size:12px'>📈 实测 STR {_real_str:.1f}% "
                                            f"({_seller_sold}售/{_seller_imp:,}展)</span>"
                                            f"<span style='color:#27ae60;font-size:11px;margin-left:8px'>✓ 我已刊同类</span></div>",
                                            unsafe_allow_html=True,
                                        )
                                    elif opp.get('seller_has_sales'):
                                        st.markdown(
                                            f"<div style='margin-top:8px'><span style='background:#27ae60;color:white;"
                                            f"padding:3px 10px;border-radius:12px;font-size:12px'>🛒 已有销售 ({_seller_sold} 件)</span></div>",
                                            unsafe_allow_html=True,
                                        )

                                    # F2.5 — 需求信号徽章（Browse-only proxy，MI 被拒后替代 STR）
                                    _ds = opp.get('demand_signal_score')
                                    _dl = opp.get('demand_signal_label', '')
                                    if _ds is not None and _dl:
                                        _ds_color = (
                                            "#27ae60" if _ds >= 75 else
                                            "#2980b9" if _ds >= 55 else
                                            "#f39c12" if _ds >= 35 else
                                            "#95a5a6"
                                        )
                                        st.markdown(
                                            f"<div style='margin-top:8px'><span style='background:{_ds_color};color:white;"
                                            f"padding:3px 10px;border-radius:12px;font-size:12px'>📊 {_dl}: {_ds}/100</span>"
                                            f"<span style='color:#999;font-size:11px;margin-left:8px'>(取代于 MI 被拒的估算值)</span></div>",
                                            unsafe_allow_html=True,
                                        )

                                    # F12 — 需求信号历史趋势（独立于 opportunity_score 趋势）
                                    if (
                                        _ds is not None and compute_field_trend is not None
                                        and _mi_history
                                    ):
                                        try:
                                            _ds_trend = compute_field_trend(
                                                _mi_history, opp.get('sku', ''), _ds,
                                                field_name="demand_signal_score",
                                            )
                                        except Exception:
                                            _ds_trend = None
                                        if _ds_trend and _ds_trend.get('delta') is not None:
                                            _ds_delta = _ds_trend['delta']
                                            if _ds_delta > 3:
                                                _dc, _da, _dsign = "#27ae60", "🌡️↑", f"+{_ds_delta}"
                                            elif _ds_delta < -3:
                                                _dc, _da, _dsign = "#e74c3c", "🌡️↓", f"{_ds_delta}"
                                            else:
                                                _dc, _da, _dsign = "#7f8c8d", "🌡️→", f"{_ds_delta:+d}"
                                            st.markdown(
                                                f"<div style='margin-top:6px'><span style='background:{_dc};color:white;"
                                                f"padding:2px 8px;border-radius:10px;font-size:11px'>"
                                                f"{_da} 市场温度 {_dsign} (vs {_ds_trend['sample_count']} 次历史)</span></div>",
                                                unsafe_allow_html=True,
                                            )

                                    # F7 — 历史趋势徽章（与历史 14 天快照对比）
                                    if compute_score_trend is not None and _mi_history:
                                        try:
                                            _trend = compute_score_trend(
                                                _mi_history,
                                                opp.get('sku', ''),
                                                opp.get('opportunity_score'),
                                                current_has_real_str=opp.get('seller_str_pct') is not None,
                                            )
                                        except Exception:
                                            _trend = None
                                        if _trend is not None:
                                            _delta = _trend.get('delta')
                                            _samples = _trend.get('sample_count', 0)
                                            _mixed = _trend.get('mixed_str_basis', False)
                                            if _samples == 0:
                                                _badge_html = (
                                                    "<span style='background:#95a5a6;color:white;"
                                                    "padding:3px 10px;border-radius:12px;font-size:12px'>🆕 首次出现</span>"
                                                )
                                            elif _delta is None:
                                                _badge_html = ""
                                            else:
                                                if _delta > 2:
                                                    _color, _arrow = "#27ae60", "📈"
                                                    _sign = f"+{_delta}"
                                                elif _delta < -2:
                                                    _color, _arrow = "#e74c3c", "📉"
                                                    _sign = f"{_delta}"
                                                else:
                                                    _color, _arrow = "#7f8c8d", "→"
                                                    _sign = f"{_delta:+d}"
                                                _badge_html = (
                                                    f"<span style='background:{_color};color:white;"
                                                    f"padding:3px 10px;border-radius:12px;font-size:12px'>"
                                                    f"{_arrow} {_sign} 分 vs 历史 ({_samples} 次样本)</span>"
                                                )
                                            if _badge_html:
                                                _caveat = (
                                                    "<span style='color:#e67e22;font-size:11px;margin-left:8px'>"
                                                    "⚠️ STR 口径变化，趋势仅供参考</span>" if _mixed else ""
                                                )
                                                # F9 — sparkline 仅在 ≥3 个数据点时显示，避免噪声
                                                _spark_html = ""
                                                _spark_data = _trend.get('sparkline') or []
                                                _valid_pts = sum(1 for v in _spark_data if v is not None)
                                                if render_sparkline is not None and _valid_pts >= 3:
                                                    _spark_str = render_sparkline(_spark_data)
                                                    _spark_html = (
                                                        f"<span style='font-family:monospace;color:#34495e;"
                                                        f"font-size:14px;margin-left:10px;letter-spacing:1px'"
                                                        f" title='历史趋势 (oldest→newest)'>{_spark_str}</span>"
                                                    )
                                                st.markdown(
                                                    f"<div style='margin-top:8px'>{_badge_html}{_spark_html}{_caveat}</div>",
                                                    unsafe_allow_html=True,
                                                )

                                    st.info(opp['recommendation'])
                        
                        # 可视化
                        st.divider()
                        st.markdown("### 📊 机会分布图")
                        
                        df = pd.DataFrame(opportunities)
                        
                        # 气泡图：成本 vs 建议价 vs 机会分数
                        fig = px.scatter(
                            df, 
                            x='dajian_cost', 
                            y='suggested_price',
                            size='opportunity_score',
                            color='margin_rate',
                            hover_name='sku',
                            hover_data=['title', 'potential_profit', 'competition'],
                            color_continuous_scale='RdYlGn',
                            title="产品机会分布 (气泡大小=机会分数, 颜色=利润率)"
                        )
                        fig.update_layout(
                            xaxis_title="大建成本 ($)",
                            yaxis_title="建议售价 ($)"
                        )
                        st.plotly_chart(fig, use_container_width=True)
                        
                        # 下载数据
                        import json
                        st.download_button(
                            "📥 下载机会列表 (JSON)",
                            json.dumps(opportunities, indent=2, ensure_ascii=False),
                            "opportunities.json",
                            "application/json"
                        )

                        # D6 — 把"洞察"接到"行动"：保存快照 + 现成 batch_publish 命令
                        st.divider()
                        st.markdown("### 🚀 加入刊登流程")

                        sku_list = [o["sku"] for o in opportunities if o.get("sku") and o.get("sku") != "N/A"]
                        ready_skus = [o["sku"] for o in opportunities if o.get("status") == "READY" and o.get("sku")]
                        pending_skus = [
                            o["sku"]
                            for o in opportunities
                            if o.get("status") in ("PENDING", "COLLECTED") and o.get("sku")
                        ]

                        col_a, col_b, col_c = st.columns(3)
                        col_a.metric("可发掘 SKU", len(sku_list))
                        col_b.metric("READY (可直接刊登)", len(ready_skus))
                        col_c.metric("待 AI 优化/起草", len(pending_skus))

                        if pending_skus:
                            st.markdown("**待 AI 优化 → 运行 `batch_analyze.py` 一次性批处理：**")
                            st.code("python batch_analyze.py", language="bash")

                        if ready_skus:
                            st.markdown("**已就绪 → 走标准 audit + publish 流程：**")
                            st.code(
                                "python scripts/audit_fix_ready_drafts.py\n"
                                + "\n".join(f"python batch_publish.py --sku {s}" for s in ready_skus[:10])
                                + ("\n# ...还有更多，见下方 SKU 列表" if len(ready_skus) > 10 else ""),
                                language="bash",
                            )

                            # F8 — 一键批量审计+发布（封装到 server.py /api/mi/batch-audit-and-publish）
                            _f8_col1, _f8_col2 = st.columns([3, 1])
                            with _f8_col1:
                                _f8_limit = st.number_input(
                                    "本次最多发布数量", min_value=1, max_value=50,
                                    value=min(10, len(ready_skus)), step=1,
                                    key="f8_max_count",
                                    help="一次审计 + 顺序发布。建议先小批 (5-10) 试水。",
                                )
                            with _f8_col2:
                                _f8_go = st.button(
                                    f"🚀 批量审计+发布 ({len(ready_skus)} 个)",
                                    key="f8_batch_run", type="primary",
                                )
                            if _f8_go:
                                try:
                                    import requests as _rq
                                    _resp = _rq.post(
                                        f"{_SERVER_BASE_URL}/api/mi/batch-audit-and-publish",
                                        json={"skus": ready_skus, "max_count": int(_f8_limit)},
                                        timeout=900,  # 单批最多 50 个 × ~15s/个
                                    )
                                    _data = _resp.json() if _resp.headers.get("content-type", "").startswith("application/json") else None
                                except Exception as _e:
                                    st.error(f"调用失败: {_e}")
                                    _data = None
                                if _data:
                                    if _data.get("status") == "ok":
                                        _c = _data.get("counters", {})
                                        st.success(
                                            f"✅ 完成 · 成功 {_c.get('published', 0)} · "
                                            f"已发布 {_c.get('already_published', 0)} · "
                                            f"审计阻塞 {_c.get('audit_blocked', 0)} · "
                                            f"跳过 {_c.get('skipped', 0)} · "
                                            f"错误 {_c.get('error', 0)}"
                                        )
                                        _audit = _data.get("audit", {})
                                        st.caption(
                                            f"审计修正 {_audit.get('changed', 0)} 项 · "
                                            f"未解决 {_audit.get('unresolved', 0)} 项 · "
                                            f"请求中被阻塞 {_audit.get('blocked_skus_in_request', 0)} 个"
                                        )
                                        with st.expander("📋 逐 SKU 结果"):
                                            st.dataframe(
                                                pd.DataFrame(_data.get("results", [])),
                                                use_container_width=True,
                                            )
                                    else:
                                        st.error(f"❌ {_data.get('message', _data)}")

                            # F13 — 异步批量发布（不阻塞 Streamlit；轮询进度）
                            st.divider()
                            st.caption("🧵 异步模式（推荐用于 >10 个 SKU；可关掉浏览器页面，进度持续运行）")
                            _f13_col1, _f13_col2 = st.columns([3, 1])
                            with _f13_col1:
                                _f13_limit = st.number_input(
                                    "异步本次最多数量", min_value=1, max_value=50,
                                    value=min(20, len(ready_skus)), step=1,
                                    key="f13_max_count",
                                )
                            with _f13_col2:
                                _f13_go = st.button(
                                    "🚀 启动异步任务", key="f13_async_run",
                                )
                            if _f13_go:
                                try:
                                    import requests as _rq
                                    _r = _rq.post(
                                        f"{_SERVER_BASE_URL}/api/mi/batch-audit-and-publish-async",
                                        json={"skus": ready_skus, "max_count": int(_f13_limit)},
                                        timeout=30,
                                    )
                                    _j = _r.json()
                                    if _j.get("job_id"):
                                        st.session_state["_f13_job_id"] = _j["job_id"]
                                        st.success(f"✅ 已入队 · job_id: `{_j['job_id']}` · 共 {_j.get('requested', 0)} 个")
                                    else:
                                        st.error(f"启动失败: {_j}")
                                except Exception as _e:
                                    st.error(f"调用失败: {_e}")

                            _active_job = st.session_state.get("_f13_job_id")
                            if _active_job:
                                _qcol1, _qcol2 = st.columns([3, 1])
                                with _qcol2:
                                    _refresh = st.button("🔄 刷新进度", key="f13_refresh")
                                with _qcol1:
                                    st.caption(f"当前任务: `{_active_job}`")
                                if _refresh or True:
                                    try:
                                        import requests as _rq
                                        _r = _rq.get(
                                            f"{_SERVER_BASE_URL}/api/mi/batch-job/{_active_job}",
                                            timeout=10,
                                        )
                                        if _r.status_code == 200:
                                            _job = _r.json()
                                            _prog = _job.get("progress", {})
                                            _done, _total = _prog.get("done", 0), _prog.get("total", 0)
                                            _pct = (_done / _total) if _total else 0
                                            st.progress(_pct, text=f"{_job.get('status', '?')} · {_done}/{_total}")
                                            _c = _job.get("counters", {})
                                            st.caption(
                                                f"成功 {_c.get('published', 0)} · "
                                                f"已发布 {_c.get('already_published', 0)} · "
                                                f"审计阻塞 {_c.get('audit_blocked', 0)} · "
                                                f"跳过 {_c.get('skipped', 0)} · "
                                                f"错误 {_c.get('error', 0)}"
                                            )
                                            if _job.get("status") == "done":
                                                with st.expander("📋 完成 · 逐 SKU 结果"):
                                                    st.dataframe(
                                                        pd.DataFrame(_job.get("results", [])),
                                                        use_container_width=True,
                                                    )
                                                # F20 — 失败重排队
                                                _err_count = _c.get("error", 0)
                                                if _err_count > 0:
                                                    if st.button(
                                                        f"🔁 重排队 {_err_count} 项失败",
                                                        key="f20_retry_errors",
                                                        type="secondary",
                                                    ):
                                                        try:
                                                            import requests as _rq2
                                                            _retry_resp = _rq2.post(
                                                                f"{_SERVER_BASE_URL}/api/mi/batch-job/{_active_job}/retry-errors",
                                                                timeout=15,
                                                            )
                                                            if _retry_resp.status_code == 200:
                                                                _new = _retry_resp.json()
                                                                if _new.get("status") == "queued":
                                                                    st.session_state["_f13_job_id"] = _new["job_id"]
                                                                    st.success(f"✅ 已重排队 {_new.get('retry_count')} 项 → 新任务 `{_new['job_id']}`")
                                                                    st.rerun()
                                                                else:
                                                                    st.info(_new.get("message", str(_new)))
                                                            else:
                                                                st.error(f"重排队失败 ({_retry_resp.status_code}): {_retry_resp.text[:200]}")
                                                        except Exception as _re:
                                                            st.error(f"重排队异常: {_re}")
                                            elif _job.get("status") == "error":
                                                st.error(f"❌ {_job.get('error')}")
                                        else:
                                            st.warning(f"任务查询失败 ({_r.status_code})；可能已过期。")
                                            st.session_state.pop("_f13_job_id", None)
                                    except Exception as _e:
                                        st.caption(f"轮询失败: {_e}")

                        if sku_list:
                            st.text_area(
                                "📋 全部推荐 SKU (复制即可)",
                                "\n".join(sku_list),
                                height=120,
                                help="可直接喂给任何接受 SKU 列表的脚本",
                            )

                        # 持久化快照供 daily_terapeak_report / 其他离线工具使用
                        try:
                            import os
                            from datetime import datetime as _dt
                            snap_dir = PROJECT_ROOT / "reports"
                            snap_dir.mkdir(exist_ok=True)
                            snap_path = snap_dir / f"mi_opportunities_{_dt.now():%Y%m%d_%H%M%S}.json"
                            snap_path.write_text(
                                json.dumps({
                                    "generated_at": _dt.now().isoformat(),
                                    "min_margin": discover_margin,
                                    "max_results": discover_limit,
                                    "opportunities": opportunities,
                                }, indent=2, ensure_ascii=False),
                                encoding="utf-8",
                            )
                            st.caption(f"📁 已保存快照到 `reports/{snap_path.name}`")
                            # F9 — 自动清理超过 30 天的旧快照（防止 reports/ 目录无限膨胀）
                            try:
                                from src.plugins.terapeak_research.history import cleanup_old_snapshots
                                _purged = cleanup_old_snapshots(keep_days=30, project_root=PROJECT_ROOT)
                                if _purged:
                                    st.caption(f"🧹 已清理 {_purged} 份超过 30 天的旧快照")
                            except Exception:
                                pass
                        except Exception as snap_err:
                            st.caption(f"⚠️ 快照保存失败: {snap_err}")
                        
                    else:
                        st.warning("⚠️ 未发现满足条件的高潜力产品")
                        st.info("""
                        **可能原因:**
                        1. 库存产品成本较高，利润空间不足
                        2. 最低利润率设置过高，尝试降低
                        3. 需要采集更多产品
                        """)
                        
                except Exception as e:
                    st.error(f"发掘失败: {e}")
                    import traceback
                    st.code(traceback.format_exc())

        # ---------- 🌐 GigaCloud 未刊登扫描 (2026-05-06 新增) ----------
        st.markdown("---")
        st.markdown("### 🌐 GigaCloud 未刊登扫描")
        st.caption(
            "上面的「发掘爆品」只评估**已经采集且非 ENDED/DELISTED** 的本地库存。"
            "下面这个按钮跳过本地数据库，直接扫 GigaCloud 收藏夹里**当前没在 eBay 售卖**的 SKU "
            "（包含 ENDED / DELISTED 旧 SKU + 还没采集过的新 SKU），找出尚未利用的机会。"
            "**不会**调用 Qwen，不会扣 token；满意就点「📥 加入待刊登」入库，让正常的 MI 流程接管。"
        )
        ext_col1, ext_col2, ext_col3, ext_col4 = st.columns(4)
        with ext_col1:
            ext_pages = st.slider(
                "扫描页数 (每页 100)", 1, 10, 5, 1,
                help="按 GigaCloud datePosted 倒序翻页，最多扫 10 页 = 1000 个候选",
                key="ext_pages",
            )
        with ext_col2:
            ext_max_results = st.slider(
                "返回数量上限", 5, 100, 30, 5, key="ext_max_results"
            )
        with ext_col3:
            ext_min_margin = st.slider(
                "最低利润率",
                0.05, 0.40, 0.15, 0.05,
                help="GigaCloud 扫描专用阈值，比上方默认更宽松",
                key="ext_min_margin",
            )
        with ext_col4:
            st.write("")
            ext_scan_btn = st.button(
                "🌐 扫 GigaCloud 未刊登机会",
                type="secondary",
                use_container_width=True,
                key="ext_scan_btn",
            )

        if ext_scan_btn:
            with st.spinner("🔍 正在拉取 GigaCloud 收藏夹并对比 eBay 市场..."):
                try:
                    from src.plugins.terapeak_research.intelligence_service import IntelligenceService
                    from src.plugins.terapeak_research.external_discovery import (
                        discover_external_opportunities,
                    )

                    _intel_ext = IntelligenceService()
                    _ext_opps, _ext_diag = discover_external_opportunities(
                        _intel_ext,
                        min_margin=ext_min_margin,
                        max_results=ext_max_results,
                        max_candidates=ext_pages * 100,
                        page_size=100,
                        max_pages=ext_pages,
                        return_diagnostics=True,
                    )
                    st.session_state["_ext_opportunities"] = _ext_opps
                    st.session_state["_ext_diagnostics"] = _ext_diag
                except Exception as ext_err:
                    st.error(f"GigaCloud 扫描失败: {ext_err}")
                    import traceback
                    st.code(traceback.format_exc())

        _ext_diag_state = st.session_state.get("_ext_diagnostics") or {}
        if _ext_diag_state:
            with st.expander("🔧 扫描漏斗诊断（看看为什么剩下这么少）", expanded=True):
                d = _ext_diag_state
                st.write({
                    "扫描收藏总数": d.get("scanned"),
                    "🚫 跳过：当前已 PUBLISHED/READY": d.get("skipped_live"),
                    "🚫 跳过：MI 黑名单": d.get("skipped_blacklist"),
                    "✅ 进入评估": d.get("candidates"),
                    "丢弃：无价格": d.get("dropped_no_price"),
                    "丢弃：无标题": d.get("dropped_no_title"),
                    "丢弃：无关键词": d.get("dropped_no_keyword"),
                    "丢弃：eBay 无市场数据": d.get("dropped_no_market"),
                    "丢弃：低于最低利润率": d.get("dropped_low_margin"),
                    "🎯 最终保留": d.get("kept"),
                })

        _ext_opps_state = st.session_state.get("_ext_opportunities") or []
        if _ext_opps_state:
            _new_count = sum(1 for o in _ext_opps_state if not o.get("existing_status"))
            _re_count = len(_ext_opps_state) - _new_count
            st.success(
                f"✅ GigaCloud 上找到 {len(_ext_opps_state)} 个机会："
                f"🆕 全新 SKU {_new_count} 个 / ♻️ 旧 SKU 复活 {_re_count} 个"
            )

            if st.button(
                f"📥 全部入库 ({len(_ext_opps_state)} 个 → PENDING)",
                key="ext_ingest_all_btn",
                type="primary",
            ):
                from src.plugins.terapeak_research.external_discovery import (
                    ingest_external_opportunity_as_pending,
                )
                _ins = _react = _skip = 0
                _errs: list = []
                for _o in _ext_opps_state:
                    try:
                        _r = ingest_external_opportunity_as_pending(_o)
                        if _r == "inserted":
                            _ins += 1
                        elif _r == "reactivated":
                            _react += 1
                        else:
                            _skip += 1
                    except Exception as _e:
                        _errs.append(f"{_o.get('sku')}: {_e}")
                st.success(
                    f"✅ 批量入库完成：新增 {_ins} / 复活 {_react} / 跳过 {_skip}"
                )
                if _errs:
                    st.error("以下 SKU 入库失败：\n" + "\n".join(_errs[:10]))

            for _idx, _opp in enumerate(_ext_opps_state):
                with st.container(border=True):
                    c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
                    with c1:
                        _badge = "🆕 全新" if not _opp.get("existing_status") else f"♻️ {_opp.get('existing_status')}"
                        st.markdown(f"**{_badge} · {_opp.get('title', '')[:120]}**")
                        st.caption(
                            f"SKU `{_opp['sku']}` · 关键词: {_opp.get('search_keywords', '')} · "
                            f"竞争: {_opp.get('competition', '?')} · "
                            f"建议: {_opp.get('recommendation', '')}"
                        )
                    c2.metric("机会分", _opp.get("opportunity_score", 0))
                    c3.metric("利润率", f"{_opp.get('margin_rate', 0)}%")
                    c4.metric("建议价", f"${_opp.get('suggested_price', 0):.2f}")
                    cc1, cc2, _ = st.columns([1, 1, 4])
                    with cc1:
                        _label = "♻️ 重激活为 PENDING" if _opp.get("existing_status") else "📥 加入待刊登"
                        if st.button(
                            _label,
                            key=f"ext_ingest_{_opp['sku']}_{_idx}",
                        ):
                            try:
                                from src.plugins.terapeak_research.external_discovery import (
                                    ingest_external_opportunity_as_pending,
                                )
                                _r = ingest_external_opportunity_as_pending(_opp)
                                if _r == "inserted":
                                    st.success(
                                        f"✅ {_opp['sku']} 已入库为 PENDING；"
                                        "下一次 MI 自动起草会处理它。"
                                    )
                                elif _r == "reactivated":
                                    st.success(
                                        f"♻️ {_opp['sku']} 已从 {_opp.get('existing_status')} "
                                        "复活为 PENDING；下一次 MI 自动起草会重新刊登它。"
                                    )
                                else:
                                    st.info(f"{_opp['sku']}: {_r}")
                            except Exception as ing_err:
                                st.error(f"入库失败: {ing_err}")
                    with cc2:
                        if _opp.get("image_url"):
                            st.markdown(f"[🖼️ 预览]({_opp['image_url']})")
        elif "_ext_opportunities" in st.session_state:
            st.info(
                "⚠️ GigaCloud 上暂无满足条件的未刊登机会。"
                "上方诊断面板会告诉你卡在哪一步——通常是「跳过：当前已 PUBLISHED/READY」吃掉了大部分收藏。"
                "可以试着：放宽利润率、扩大扫描页数、或先用 `scripts/auto_favorite_gigacloud.py` 多收藏一些新品。"
            )

    # ========== Tab 2: 大建收藏分析 (新增) ==========
    with tab2:
        st.subheader("⭐ 大建收藏产品分析")
        
        st.markdown("""
        分析您在大建云仓收藏的产品，通过 Terapeak 市场数据，
        发现**利润高、好卖**的产品，一键锁定对应大建链接。
        """)
        
        with st.expander("ℹ️ 如何使用收藏分析？", expanded=False):
            st.markdown("""
            ### 工作流程
            1. 在大建云仓网站收藏感兴趣的产品
            2. 点击「获取收藏」调用大建API获取收藏列表
            3. 系统自动分析每个产品的eBay市场数据
            4. 推荐利润最高的产品进行刊登
            
            ### 前提条件
            - 需要配置大建API凭证 (DAJIAN_API_KEY, DAJIAN_API_SECRET)
            - 需要在大建云仓有收藏的产品
            """)
        
        col1, col2, col3 = st.columns([1, 1, 1])
        with col1:
            fav_margin = st.slider("最低利润率", 0.10, 0.40, 0.15, 0.05, key="fav_margin")
        with col2:
            analyze_limit = st.selectbox("分析数量", [20, 50, 100, 200], index=1, key="analyze_limit",
                                        help="分析更多产品需要更长时间")
        with col3:
            fav_btn = st.button("⭐ 分析收藏产品", type="primary", use_container_width=True, key="fav_btn")
        
        if fav_btn:
            with st.spinner("🔍 正在获取收藏产品并分析市场..."):
                try:
                    import os
                    from src.clients.dajian_client import DaJianClient
                    
                    client_id = os.getenv("DAJIAN_API_KEY")
                    client_secret = os.getenv("DAJIAN_API_SECRET")
                    
                    if not client_id or not client_secret:
                        st.error("⚠️ 未配置大建API凭证，请在 .env 文件中设置 DAJIAN_API_KEY 和 DAJIAN_API_SECRET")
                        st.stop()
                    
                    dajian = DaJianClient(client_id, client_secret)
                    
                    # 测试连接
                    if not dajian.test_connection():
                        st.warning("⚠️ 大建API连接失败，尝试使用备用方法...")
                    
                    # 获取收藏
                    favorites = dajian.get_favorites()
                    
                    if not favorites:
                        st.warning("📭 未找到收藏产品。请先在大建云仓网站收藏一些产品。")
                        st.info("**提示:** 如果已收藏产品但仍显示空，可能是API权限问题。")
                        st.stop()
                    
                    st.success(f"✅ 获取到 {len(favorites)} 个收藏产品")
                    
                    # 补全收藏价格（部分收藏记录不含 price 字段）
                    def _pick_price(price_info: dict) -> float:
                        if not price_info:
                            return 0
                        return float(
                            price_info.get("exclusivePrice") or
                            price_info.get("discountedPrice") or
                            price_info.get("price") or 0
                        )
                    
                    price_map = {}
                    skus = []
                    for fav in favorites:
                        sku = fav.get("sku") or fav.get("skuCode") or fav.get("productSku")
                        if sku:
                            skus.append(sku)
                    
                    # 分批查询价格（每批最多 200）
                    if skus:
                        for i in range(0, len(skus), 200):
                            batch = skus[i:i + 200]
                            try:
                                price_list = dajian.get_product_prices(batch)
                                for p in price_list:
                                    if p.get("sku"):
                                        price_map[p["sku"]] = p
                            except Exception:
                                continue
                    
                    # 限制分析数量
                    favorites_to_analyze = favorites[:analyze_limit]
                    st.info(f"📊 将分析前 {len(favorites_to_analyze)} 个收藏产品 (共 {len(favorites)} 个)")
                    
                    # 分析每个收藏产品
                    from src.plugins.terapeak_research.research_client import TerapeakClient
                    terapeak = TerapeakClient()
                    
                    opportunities = []
                    progress = st.progress(0)
                    status_text = st.empty()
                    
                    for i, fav in enumerate(favorites_to_analyze):
                        progress.progress((i + 1) / len(favorites_to_analyze))
                        
                        title = (
                            fav.get("title", "") or
                            fav.get("name", "") or
                            fav.get("productName", "") or
                            fav.get("productTitle", "")
                        )
                        if not title:
                            continue
                        
                        # 提取关键词
                        keywords = _extract_search_keywords(title)
                        if not keywords:
                            continue
                        
                        status_text.text(f"分析中 ({i+1}/{len(favorites_to_analyze)}): {keywords[:30]}...")
                        
                        try:
                            # 获取市场数据 (限制请求量)
                            products = terapeak.search_products(keywords, limit=20, min_price=50, buy_it_now_only=True)
                            if not products:
                                continue
                            
                            prices = [p['price'] for p in products if p.get('price', 0) > 0]
                            if not prices:
                                continue
                            
                            avg_price = sum(prices) / len(prices)
                            sku = fav.get("sku") or fav.get("skuCode") or fav.get("productSku")
                            price_info = price_map.get(sku, {}) if sku else {}
                            cost = fav.get("price", 0) or fav.get("cost", 0) or _pick_price(price_info) or 100
                            
                            # 计算利润
                            margin = (avg_price - cost) / avg_price if avg_price > cost else 0
                            
                            if margin >= fav_margin:
                                opportunities.append({
                                    "sku": fav.get("sku") or fav.get("skuCode") or fav.get("productSku") or "N/A",
                                    "title": title,
                                    "dajian_cost": cost,
                                    "market_avg_price": avg_price,
                                    "margin": margin,
                                    "potential_profit": avg_price - cost,
                                    "url": fav.get("url", fav.get("detailUrl", "")),
                                    "keywords": keywords,
                                    "image_url": _extract_thumb(fav),
                                })
                        except Exception as e:
                            continue
                    
                    progress.empty()
                    status_text.empty()
                    
                    if opportunities:
                        # 按利润排序
                        opportunities.sort(key=lambda x: x['potential_profit'], reverse=True)
                        
                        st.success(f"🎯 发现 {len(opportunities)} 个有潜力的收藏产品！")
                        
                        # 显示统计
                        col1, col2, col3 = st.columns(3)
                        col1.metric("发现机会", len(opportunities))
                        col2.metric("平均利润率", f"{sum(o['margin'] for o in opportunities)/len(opportunities)*100:.1f}%")
                        col3.metric("潜在总利润", f"${sum(o['potential_profit'] for o in opportunities):,.0f}")
                        
                        st.divider()
                        
                        # 产品列表
                        for i, opp in enumerate(opportunities[:10], 1):
                            with st.expander(f"#{i} {opp['sku']} - 利润: ${opp['potential_profit']:.0f}"):
                                thumb_col, col1, col2 = st.columns([1, 2, 1])

                                with thumb_col:
                                    _render_thumb(opp, width=140)

                                with col1:
                                    st.write(f"**产品:** {opp['title']}")
                                    st.write(f"**搜索词:** `{opp['keywords']}`")
                                    if opp.get('url'):
                                        st.markdown(f"[🔗 大建产品链接]({opp['url']})")
                                
                                with col2:
                                    st.metric("成本", f"${opp['dajian_cost']:.2f}")
                                    st.metric("市场均价", f"${opp['market_avg_price']:.2f}")
                                    st.metric("利润率", f"{opp['margin']*100:.1f}%")
                        
                    else:
                        st.warning("⚠️ 未找到满足利润要求的产品，尝试降低利润率要求。")
                    
                except Exception as e:
                    st.error(f"分析失败: {e}")
                    import traceback
                    st.code(traceback.format_exc())
    
    # ========== Tab 3: 趋势发掘 (新增) ==========
    with tab3:
        st.subheader("📈 市场趋势发掘")
        
        st.markdown("""
        **主动发现**市场上热门但您**尚未采集**的产品品类。
        不依赖现有库存，帮您发现新的选品方向。
        """)
        
        # 高 STR 类目选择
        st.markdown("#### 🔥 高 Sell-Through 类目快捷发掘")
        str_col1, str_col2, str_col3 = st.columns([2, 1, 1])
        
        with str_col1:
            str_category = st.selectbox(
                "选择类目",
                ["全部高STR类目", "sofas - 沙发类", "beds - 床类", "storage - 储物家具", 
                 "office - 办公家具", "outdoor - 户外家具", "dining - 餐厅家具", 
                 "entertainment - 娱乐家具"],
                key="str_category"
            )
        
        with str_col2:
            st.write("")
            high_str_btn = st.button("🚀 发掘高STR类目", type="primary", use_container_width=True, key="high_str_btn")
        
        with str_col3:
            st.write("")
            if st.button("ℹ️ 什么是STR", key="str_info"):
                st.info("**Sell-Through Rate (STR)** = 已售数量 / 刊登数量 × 100%。STR越高说明该品类需求旺盛、竞争相对较低，更容易销售。")
        
        st.divider()
        
        col1, col2 = st.columns([3, 1])
        with col1:
            trend_keywords = st.text_area(
                "自定义关键词 (每行一个，留空使用默认热门词)",
                placeholder="sectional sofa\nplatform bed\ngaming chair\ncoffee table",
                height=100,
                key="trend_keywords"
            )
        with col2:
            st.write("")
            trend_btn = st.button("🔍 发现趋势", type="primary", use_container_width=True, key="trend_btn")
        
        # 处理高 STR 类目发掘
        if high_str_btn:
            with st.spinner("🔍 正在发掘高 Sell-Through 类目..."):
                try:
                    from src.plugins.terapeak_research.trend_discovery import MarketTrendDiscovery
                    
                    discovery = MarketTrendDiscovery()
                    
                    # 解析选择的类目
                    if str_category == "全部高STR类目":
                        keywords = discovery.get_high_str_keywords()
                        st.info(f"📊 正在分析 {len(keywords)} 个高 STR 关键词...")
                    else:
                        cat_key = str_category.split(" - ")[0]
                        keywords = discovery.get_high_str_keywords(cat_key)
                        st.info(f"📊 正在分析 {cat_key} 类目的 {len(keywords)} 个关键词...")
                    
                    opportunities = discovery.discover_opportunities(keywords=keywords, include_dajian_favorites=False)
                    
                    if opportunities:
                        st.success(f"✅ 发现 {len(opportunities)} 个高 STR 市场机会！")
                        
                        # 统计
                        high_opp = [o for o in opportunities if o.opportunity_score >= 70]
                        avg_str = sum(o.sell_through_rate for o in opportunities) / len(opportunities) if opportunities else 0
                        
                        col1, col2, col3, col4 = st.columns(4)
                        col1.metric("市场机会", len(opportunities))
                        col2.metric("高潜力", len(high_opp), "🔥")
                        col3.metric("预估平均STR%", f"{avg_str:.1f}%", help="基于类目经验区间估算，非实时销售数据")
                        col4.metric("平均需求分", f"{sum(o.demand_score for o in opportunities)/len(opportunities):.0f}")
                        
                        st.divider()
                        st.markdown("### 💡 高 STR 市场机会列表")
                        
                        for i, opp in enumerate(opportunities, 1):
                            score_emoji = "🔥" if opp.opportunity_score >= 70 else "✅" if opp.opportunity_score >= 50 else "⚠️"
                            str_badge = f"预估STR {opp.sell_through_rate:.0f}%"
                            
                            with st.expander(f"{score_emoji} {opp.keyword} | {str_badge} | 机会分: {opp.opportunity_score}"):
                                col1, col2 = st.columns([2, 1])
                                
                                with col1:
                                    st.write(f"**品类:** {opp.category}")
                                    st.write(f"**竞争程度:** {opp.competition}")
                                    st.write(f"**价格区间:** ${opp.suggested_price_range[0]:.0f} - ${opp.suggested_price_range[1]:.0f}")
                                    
                                    # eBay 搜索链接
                                    if opp.ebay_search_url:
                                        st.markdown(f"[🔍 查看 eBay 竞品]({opp.ebay_search_url})")
                                    
                                    if opp.key_item_specifics:
                                        st.write("**推荐 Item Specifics:**")
                                        for k, v in opp.key_item_specifics.items():
                                            st.write(f"  - {k}: {v}")
                                
                                with col2:
                                    st.metric("预估 STR*", f"{opp.sell_through_rate:.0f}%", help="*基于类目经验区间估算。需要 Marketplace Insights API 权限才能拿到真实 Sell-Through Rate。")
                                    st.metric("市场均价", f"${opp.market_avg_price:.2f}")
                                    st.metric("需求分数", opp.demand_score)
                                    st.metric("机会分数", opp.opportunity_score)
                                
                                if opp.listing_tips:
                                    st.divider()
                                    st.write("**刊登建议:**")
                                    for tip in opp.listing_tips:
                                        st.write(tip)
                    else:
                        st.warning("未发现市场机会，请尝试其他类目")
                        
                except Exception as e:
                    st.error(f"发掘失败: {e}")
                    import traceback
                    st.code(traceback.format_exc())
        
        if trend_btn:
            with st.spinner("🔍 正在分析市场趋势..."):
                try:
                    from src.plugins.terapeak_research.trend_discovery import MarketTrendDiscovery
                    
                    discovery = MarketTrendDiscovery()
                    
                    keywords = None
                    if trend_keywords.strip():
                        keywords = [k.strip() for k in trend_keywords.strip().split('\n') if k.strip()]
                    
                    opportunities = discovery.discover_opportunities(keywords=keywords, include_dajian_favorites=False)
                    
                    if opportunities:
                        st.success(f"✅ 发现 {len(opportunities)} 个市场机会！")
                        
                        # 统计
                        high_opp = [o for o in opportunities if o.opportunity_score >= 70]
                        avg_str = sum(o.sell_through_rate for o in opportunities) / len(opportunities) if opportunities else 0
                        
                        col1, col2, col3, col4 = st.columns(4)
                        col1.metric("市场机会", len(opportunities))
                        col2.metric("高潜力", len(high_opp), "🔥")
                        col3.metric("预估平均STR", f"{avg_str:.1f}%")
                        col4.metric("平均需求分", f"{sum(o.demand_score for o in opportunities)/len(opportunities):.0f}")
                        
                        st.divider()
                        st.markdown("### 💡 市场机会列表")
                        
                        for i, opp in enumerate(opportunities, 1):
                            score_emoji = "🔥" if opp.opportunity_score >= 70 else "✅" if opp.opportunity_score >= 50 else "⚠️"
                            str_badge = f"预估STR {opp.sell_through_rate:.0f}%" if opp.sell_through_rate > 0 else ""
                            
                            with st.expander(f"{score_emoji} {opp.keyword} | {str_badge} | 机会分: {opp.opportunity_score}"):
                                col1, col2 = st.columns([2, 1])
                                
                                with col1:
                                    st.write(f"**品类:** {opp.category}")
                                    st.write(f"**竞争程度:** {opp.competition}")
                                    st.write(f"**价格区间:** ${opp.suggested_price_range[0]:.0f} - ${opp.suggested_price_range[1]:.0f}")
                                    
                                    # eBay 搜索链接
                                    if opp.ebay_search_url:
                                        st.markdown(f"[🔍 查看 eBay 竞品]({opp.ebay_search_url})")
                                    
                                    if opp.key_item_specifics:
                                        st.write("**推荐 Item Specifics:**")
                                        for k, v in opp.key_item_specifics.items():
                                            st.write(f"  - {k}: {v}")
                                
                                with col2:
                                    if opp.sell_through_rate > 0:
                                        st.metric("预估 STR*", f"{opp.sell_through_rate:.0f}%", help="*基于类目经验区间估算。需要 Marketplace Insights API 权限才能拿到真实数据。")
                                    st.metric("市场均价", f"${opp.market_avg_price:.2f}")
                                    st.metric("需求分数", opp.demand_score)
                                    st.metric("机会分数", opp.opportunity_score)
                                
                                if opp.listing_tips:
                                    st.divider()
                                    st.write("**刊登建议:**")
                                    for tip in opp.listing_tips:
                                        st.write(tip)
                                
                                # 匹配的大建产品
                                if opp.dajian_matches:
                                    st.divider()
                                    st.write(f"**匹配到 {len(opp.dajian_matches)} 个大建产品:**")
                                    for match in opp.dajian_matches[:3]:
                                        m_thumb, m_info = st.columns([1, 5])
                                        with m_thumb:
                                            _render_thumb(match, width=90)
                                        with m_info:
                                            st.write(f"- **{match.get('sku', '')}** · {match.get('title', '')[:60]}")
                                            if match.get('url'):
                                                st.markdown(f"  [🔗 查看大建链接]({match['url']})")
                    else:
                        st.warning("未发现市场机会，请尝试其他关键词")
                    
                except Exception as e:
                    st.error(f"趋势发掘失败: {e}")
                    import traceback
                    st.code(traceback.format_exc())
    
    # ========== Tab 4: 关键词分析 ==========
    with tab4:
        st.subheader("关键词市场分析")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            keyword = st.text_input("输入关键词", placeholder="例如: sofa, coffee table")
        with col2:
            analyze_btn = st.button("🔍 分析", type="primary", use_container_width=True)
        
        if analyze_btn and keyword:
            with st.spinner("正在分析市场数据..."):
                try:
                    from src.plugins.terapeak_research.intelligence_service import IntelligenceService
                    intel = IntelligenceService()
                    
                    market = intel.analyze_market(keyword)
                    pain_points = intel.analyze_competitor_pain_points(keyword)
                    
                    # 显示市场概览
                    st.markdown("### 📊 市场概览")
                    
                    col1, col2, col3, col4 = st.columns(4)
                    col1.metric("平均价格", f"${market.avg_price:.2f}")
                    col2.metric("价格区间", f"${market.min_price:.0f} - ${market.max_price:.0f}")
                    col3.metric("竞争程度", market.competition_level.upper())
                    col4.metric("商品数量", market.total_listings)
                    
                    # 竞争程度指示器
                    competition_colors = {
                        'low': '🟢', 'medium': '🟡', 
                        'high': '🟠', 'very_high': '🔴'
                    }
                    st.info(f"{competition_colors.get(market.competition_level, '⚪')} 竞争评分: {market.competition_score}/100")
                    
                    # 价格分布图
                    if market.avg_price > 0:
                        fig = go.Figure()
                        fig.add_trace(go.Indicator(
                            mode="gauge+number+delta",
                            value=market.avg_price,
                            title={'text': "市场均价"},
                            delta={'reference': market.median_price},
                            gauge={
                                'axis': {'range': [market.min_price, market.max_price]},
                                'bar': {'color': "#1f77b4"},
                                'steps': [
                                    {'range': [market.min_price, market.median_price], 'color': "#e8f4f8"},
                                    {'range': [market.median_price, market.max_price], 'color': "#d4e6f1"}
                                ],
                                'threshold': {
                                    'line': {'color': "red", 'width': 4},
                                    'thickness': 0.75,
                                    'value': market.median_price
                                }
                            }
                        ))
                        fig.update_layout(height=300)
                        st.plotly_chart(fig, use_container_width=True)
                    
                    # 类目通用痛点参考 (模板库)
                    st.markdown("### ⚠️ 类目通用痛点参考")
                    st.caption("以下为家具品类的通用痛点模板，仅侜文案参考，非实时从实际竞品评论中抽取。")
                    for pp in pain_points:
                        with st.expander(f"❌ {pp.issue} (频率: {pp.frequency}%)"):
                            st.write(f"**我们的优势:** {pp.our_advantage}")
                            st.code(pp.suggested_emphasis, language=None)
                    
                except Exception as e:
                    st.error(f"分析失败: {e}")
    
    # ========== Tab 5: Item Specifics 建议 (新增) ==========
    with tab5:
        st.subheader("📋 Item Specifics 建议")
        
        st.markdown("""
        获取高需求的 **Item Specifics** 属性建议，帮助您的产品在搜索中排名更高。
        """)
        
        col1, col2 = st.columns([3, 1])
        with col1:
            spec_keyword = st.text_input(
                "输入产品关键词",
                placeholder="例如: sectional sofa, gaming chair",
                key="spec_keyword"
            )
        with col2:
            st.write("")
            spec_btn = st.button("📋 获取建议", type="primary", use_container_width=True, key="spec_btn")
        
        if spec_btn and spec_keyword:
            with st.spinner("🔍 正在分析 Item Specifics..."):
                try:
                    from src.plugins.terapeak_research.trend_discovery import MarketTrendDiscovery
                    
                    discovery = MarketTrendDiscovery()
                    advice = discovery.get_listing_recommendation(spec_keyword)
                    
                    if "error" in advice:
                        st.error(advice["error"])
                    else:
                        st.success(f"✅ 已生成 {spec_keyword} 的刊登建议")
                        
                        # 市场分析
                        st.markdown("### 📊 市场分析")
                        ma = advice["market_analysis"]
                        col1, col2, col3, col4 = st.columns(4)
                        col1.metric("市场均价", f"${ma['avg_price']:.2f}")
                        col2.metric("建议价格区间", f"${ma['price_range'][0]:.0f}-${ma['price_range'][1]:.0f}")
                        col3.metric("竞争程度", ma['competition'].upper())
                        col4.metric("需求分数", ma['demand_score'])
                        
                        st.divider()
                        
                        # Item Specifics
                        st.markdown("### 📋 必填 Item Specifics")
                        if advice["required_item_specifics"]:
                            for k, v in advice["required_item_specifics"].items():
                                st.write(f"- **{k}:** `{v}`")
                        else:
                            st.info("暂无特定必填项")
                        
                        st.markdown("### 💡 推荐 Item Specifics 选项")
                        if advice["recommended_item_specifics"]:
                            for k, values in advice["recommended_item_specifics"].items():
                                st.write(f"**{k}:** {', '.join(values)}")
                        else:
                            st.info("暂无推荐选项")
                        
                        st.divider()
                        
                        # 标题模板
                        st.markdown("### ✏️ 标题模板")
                        for i, template in enumerate(advice["title_templates"], 1):
                            st.code(template, language=None)
                        
                        # 定价策略
                        st.markdown("### 💰 定价策略")
                        ps = advice["pricing_strategy"]
                        col1, col2, col3 = st.columns(3)
                        col1.metric("底价", f"${ps['floor_price']:.2f}")
                        col2.metric("推荐价", f"${ps['recommended_price']:.2f}")
                        col3.metric("天花板", f"${ps['ceiling_price']:.2f}")
                        
                        # 刊登建议
                        if advice["listing_tips"]:
                            st.markdown("### 📝 刊登建议")
                            for tip in advice["listing_tips"]:
                                st.write(tip)
                        
                except Exception as e:
                    st.error(f"获取建议失败: {e}")
                    import traceback
                    st.code(traceback.format_exc())
    
    # ========== Tab 6: 智能定价 ==========
    with tab6:
        st.subheader("💰 智能定价计算器")
        
        col1, col2 = st.columns(2)
        
        with col1:
            total_cost = st.number_input("大建总成本 ($)", min_value=0.0, value=150.0, step=10.0)
            market_price = st.number_input("市场均价 ($)", min_value=0.0, value=250.0, step=10.0)
        
        with col2:
            min_m = st.slider("最低利润率", 0.05, 0.25, 0.10, 0.01)
            max_m = st.slider("最高利润率", 0.20, 0.50, 0.35, 0.01)
        
        if st.button("💵 计算智能价格", type="primary"):
            try:
                from src.services.pricing_engine import PricingEngine
                
                result = PricingEngine.calculate_smart_price(
                    total_cost=total_cost,
                    market_price=market_price,
                    min_margin=min_m,
                    max_margin=max_m
                )
                
                st.markdown("### 📊 定价结果")
                
                col1, col2, col3 = st.columns(3)
                col1.metric("建议售价", f"${result['final_price']:.2f}")
                col2.metric("实际利润率", f"{result['margin']*100:.1f}%")
                col3.metric("定价策略", result['strategy'])
                
                # 价格区间可视化
                fig = go.Figure()
                
                # 添加价格区间
                fig.add_trace(go.Scatter(
                    x=['保底价', '竞争价', '最终价', '天花板'],
                    y=[result['floor_price'], result['competitive_price'], 
                       result['final_price'], result['ceiling_price']],
                    mode='lines+markers+text',
                    text=[f"${result['floor_price']:.0f}", 
                          f"${result['competitive_price']:.0f}",
                          f"${result['final_price']:.0f}",
                          f"${result['ceiling_price']:.0f}"],
                    textposition='top center',
                    line=dict(color='#1f77b4', width=3),
                    marker=dict(size=12)
                ))
                
                # 添加市场价参考线
                fig.add_hline(y=market_price, line_dash="dash", 
                             line_color="red", annotation_text=f"市场均价 ${market_price:.0f}")
                
                # 添加成本线
                fig.add_hline(y=total_cost, line_dash="dot",
                             line_color="gray", annotation_text=f"成本 ${total_cost:.0f}")
                
                fig.update_layout(
                    title="定价策略分析",
                    yaxis_title="价格 ($)",
                    height=400
                )
                
                st.plotly_chart(fig, use_container_width=True)
                
                # 策略说明
                strategy_desc = {
                    'FLOOR_PRICE': "⚠️ 市场价较低，使用保底价保证最低利润",
                    'CEILING_PRICE': "🔥 市场价很高，获取最大利润空间",
                    'COMPETITIVE': "✅ 比市场价低5%，获取竞争优势",
                    'STANDARD': "📊 无市场数据，使用标准定价"
                }
                st.info(strategy_desc.get(result['strategy'], result['strategy']))
                
            except Exception as e:
                st.error(f"计算失败: {e}")


def _extract_search_keywords(title: str) -> str:
    """从产品标题中提取搜索关键词"""
    import re
    
    if not title:
        return ""
    
    # 移除常见噪音
    noise_patterns = [
        r'\[Video\]', r'\[video\]',
        r'\d+(\.\d+)?\s*("|inch|inches|cm|mm)',  # 尺寸
        r'\d+(\.\d+)?\s*(lbs?|kg|pounds?)',  # 重量
        r'[A-Z]{2,3}\d+[A-Z]?',  # SKU模式
        r'^(HOMFUL|homful|Homful)\s*',  # 品牌前缀
        r'^\d+\s*',  # 开头数字
        r'FREE SHIPPING',
        r'New\s+',
    ]
    
    clean = title
    for pattern in noise_patterns:
        clean = re.sub(pattern, '', clean, flags=re.IGNORECASE)
    
    # 提取核心词
    words = clean.split()
    
    # 保留前4-6个有意义的词
    meaningful = []
    for word in words:
        word = word.strip('.,!?()[]{}')
        if len(word) >= 3 and not word.isdigit():
            meaningful.append(word)
            if len(meaningful) >= 5:
                break
    
    return ' '.join(meaningful)


if __name__ == "__main__":
    st.set_page_config(
        page_title="Market Intelligence",
        page_icon="🎯",
        layout="wide"
    )
    render_market_intelligence()
