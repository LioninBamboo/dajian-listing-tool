"""💰 财务 F0/F1 — 订单级预估损益 + GIGA 履约看板."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = PROJECT_ROOT / "ebay_collection.db"


def render_finance_dashboard():
    st.title("💰 财务")
    st.caption(
        "eBay 已付款订单 × GIGA 采购成本快照 × 预估平台费 × 履约阶段。"
        " 成本基数 = GIGA 订单（货+运+保险+支付宝），不是售价。"
        " 费用为估算（FVF+广告+固定费），标「估」。"
        " **全额/部分退款不计入 GMV/净利。**"
    )

    db_path = DEFAULT_DB
    if not db_path.exists():
        st.error(f"数据库不存在: {db_path}")
        return

    col_a, col_b = st.columns([1, 3])
    with col_a:
        days = st.number_input("同步近 N 天", min_value=1, max_value=120, value=30, step=1)
        ad_rate = st.slider("预估广告率", 0.0, 0.15, 0.05, 0.01)
        if st.button("🔄 从 eBay 同步订单", type="primary", use_container_width=True):
            with st.spinner("拉取 eBay 订单并计算 PnL…"):
                try:
                    from src.services.finance_orders import sync_orders_from_ebay

                    report = sync_orders_from_ebay(
                        days=int(days),
                        db_path=str(db_path),
                        ad_rate=float(ad_rate),
                    )
                    st.success(
                        f"完成：拉取 {report['orders_pulled']}，"
                        f"写入 {report['upserted']}，错误 {report['errors']}，"
                        f"GIGA 关联更新 {report.get('giga_links_updated', 0)}"
                    )
                except Exception as e:
                    st.error(f"同步失败: {e}")
        if st.button("🔗 仅刷新 GIGA 关联", use_container_width=True):
            try:
                from src.services.finance_orders import refresh_giga_links, ensure_finance_tables

                conn_r = sqlite3.connect(str(db_path))
                ensure_finance_tables(conn_r)
                rep = refresh_giga_links(conn_r)
                conn_r.close()
                st.success(f"已更新 giga_order_no: {rep.get('updated', 0)} 行")
            except Exception as e:
                st.error(f"刷新失败: {e}")

    from src.services.finance_orders import (
        FINANCE_RANGE_KEYS,
        FINANCE_RANGE_LABELS_ZH,
        FULFILL_STAGE_LABELS_ZH,
        ensure_finance_tables,
        query_finance_lines_for_order,
        query_finance_orders,
        query_finance_summary,
        refresh_giga_links,
        resolve_finance_date_range,
    )

    conn = sqlite3.connect(str(db_path))
    ensure_finance_tables(conn)
    try:
        refresh_giga_links(conn)
    except Exception:
        pass

    st.subheader("📊 总览（仅 PAID）")
    range_key = st.radio(
        "时间范围",
        options=list(FINANCE_RANGE_KEYS),
        format_func=lambda k: FINANCE_RANGE_LABELS_ZH.get(k, k),
        horizontal=True,
        index=list(FINANCE_RANGE_KEYS).index("30d"),
        help="总览与明细均按订单日期切片。默认近30天。",
    )
    bounds = resolve_finance_date_range(range_key)
    if bounds.get("date_from"):
        st.caption(
            f"筛选：{bounds['label']} "
            f"（{bounds['date_from']} ~ {bounds['date_to'] or '…'}）· 退款单不进利润"
        )
    else:
        st.caption(f"筛选：{bounds['label']} · 退款单不进利润")

    summary = query_finance_summary(conn, range_key=range_key)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("订单数", summary["order_count"])
    c2.metric("GMV", f"${summary['gmv']:.2f}")
    c3.metric("COGS (GIGA)", f"${summary['cogs']:.2f}")
    c4.metric("费用(估)", f"${summary['fees']:.2f}")
    c5.metric(
        "净利(估)",
        f"${summary['net']:.2f}",
        delta_color="normal" if summary["net"] >= 0 else "inverse",
    )
    c6.metric(
        "利润率(估)",
        f"{summary['margin'] * 100:.1f}%",
        delta_color="normal" if summary["margin"] >= 0 else "inverse",
    )

    f1, f2, f3, f4, f5 = st.columns(5)
    f1.metric("未推 GIGA", summary.get("not_pushed", 0))
    f2.metric("已推未付", summary.get("pushed_unpaid", 0))
    stages = summary.get("fulfill_stages") or {}
    f3.metric("处理中", stages.get("processing", 0))
    f4.metric("已发货", summary.get("shipped", 0))
    f5.metric("履约异常", stages.get("error", 0))

    if summary["loss_orders"]:
        st.warning(f"💣 预估亏损订单: **{summary['loss_orders']}** 单（本范围内 PAID）")
    if summary.get("not_pushed"):
        st.info(f"📦 有 **{summary['not_pushed']}** 单 PAID 尚未推送 GIGA")
    refund_n = summary.get("refund_orders") or 0
    if refund_n:
        st.caption(
            f"↩ 退款提示（不计入上表）：**{refund_n}** 单 "
            f"FULLY/PARTIALLY_REFUNDED，原成交额合计 "
            f"**${float(summary.get('refund_gmv') or 0):.2f}**"
        )

    st.markdown("---")
    st.subheader("📋 订单明细")
    filt1, filt2, filt3 = st.columns(3)
    with filt1:
        only_loss = st.checkbox("只看亏损单", value=False)
    with filt2:
        stage_options = ["（全部）"] + list(FULFILL_STAGE_LABELS_ZH.keys())
        stage_pick = st.selectbox(
            "履约阶段",
            stage_options,
            format_func=lambda k: "（全部）" if k == "（全部）" else FULFILL_STAGE_LABELS_ZH.get(k, k),
        )
    with filt3:
        show_refunds = st.checkbox("只看退款单", value=False, help="退款不进总览利润，仅明细查看")

    stage_filter = None if stage_pick == "（全部）" else stage_pick
    orders = query_finance_orders(
        conn,
        only_loss=only_loss and not show_refunds,
        fulfill_stage=stage_filter if not show_refunds else None,
        range_key=range_key,
        payment_status=None if show_refunds else "PAID",
        include_refunds_only=show_refunds,
        limit=300,
    )

    if not orders:
        st.info("本范围内暂无订单。可扩大时间范围，或点击上方「从 eBay 同步订单」。")
        conn.close()
        return

    # table view
    table_rows = []
    for o in orders:
        table_rows.append(
            {
                "订单号": o.get("platform_order_id"),
                "日期": (o.get("order_date") or "")[:16],
                "支付": o.get("payment_status"),
                "eBay履约": o.get("fulfillment_status"),
                "GIGA阶段": o.get("fulfill_stage_label") or "",
                "GIGA本地状态": o.get("giga_local_status") or "",
                "GMV": o.get("gross_sales"),
                "COGS": o.get("total_cogs"),
                "费(估)": o.get("total_fees_est"),
                "净利(估)": o.get("net_est"),
                "利润率": f"{(o.get('margin_est') or 0)*100:.1f}%",
                "GIGA单号": o.get("giga_order_no_live") or o.get("giga_order_no") or "",
            }
        )
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("🔍 订单行明细")
    order_ids = [o["platform_order_id"] for o in orders]
    pick = st.selectbox("选择订单", order_ids)
    if pick:
        lines = query_finance_lines_for_order(conn, pick)
        head = next((o for o in orders if o["platform_order_id"] == pick), {})
        net = head.get("net_est") or 0
        stage_label = head.get("fulfill_stage_label") or "—"
        giga_no = head.get("giga_order_no_live") or head.get("giga_order_no") or "—"
        pay = head.get("payment_status") or ""
        if str(pay).upper() in ("FULLY_REFUNDED", "PARTIALLY_REFUNDED"):
            st.warning(
                f"退款单 **{pay}** · 原成交 ${float(head.get('gross_sales') or 0):.2f} · "
                f"不计入总览利润 · 履约: {stage_label} · GIGA: {giga_no}"
            )
        elif net < 0:
            st.error(f"预估净利 **${net:.2f}**（亏损） · 履约: {stage_label} · GIGA: {giga_no}")
        else:
            st.success(f"预估净利 **${net:.2f}** · 履约: {stage_label} · GIGA: {giga_no}")
        if head.get("giga_last_error"):
            st.warning(f"GIGA 最近错误: {head.get('giga_last_error')}")

        line_rows = []
        for ln in lines:
            line_rows.append(
                {
                    "SKU": ln.get("sku"),
                    "Item#": ln.get("ebay_item_number"),
                    "Trans#": ln.get("ebay_transaction_id"),
                    "Qty": ln.get("qty"),
                    "成交": ln.get("line_gross"),
                    "单位GIGA成本": ln.get("unit_giga_cost"),
                    "行COGS": ln.get("line_cogs"),
                    "费(估)": ln.get("fee_est"),
                    "净利(估)": ln.get("net_est"),
                    "成本来源": ln.get("cost_source"),
                }
            )
        st.dataframe(line_rows, use_container_width=True, hide_index=True)
        st.caption(
            "Item# = eBay Item Number；Trans# = eBay Transaction ID（与 GIGA 推单字段一致）。"
            " GIGA阶段：未推 / 已推未付 / 处理中 / 已发货 / 异常。"
            " 利润率(估) = 净利(估) / GMV。"
        )

    conn.close()
