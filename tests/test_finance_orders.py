"""Finance F0/F1: order PnL tables, fee/cost math, GIGA fulfill stage, email HTML."""
from __future__ import annotations

import json
import sqlite3

from datetime import datetime

from src.services.finance_orders import (
    build_lines_from_ebay_order,
    classify_giga_fulfill_stage,
    ensure_finance_tables,
    estimate_line_fees,
    query_finance_orders,
    query_finance_summary,
    refresh_giga_links,
    render_finance_email_html,
    resolve_finance_date_range,
    upsert_finance_order,
)
from src.services.giga_dropship import ensure_fulfillment_table, record_fulfillment_attempt
from src.services.pricing_engine import PricingEngine


SAMPLE = {
    "orderId": "13-15010-93245",
    "creationDate": "2026-08-11T02:26:09.000Z",
    "orderPaymentStatus": "PAID",
    "orderFulfillmentStatus": "NOT_STARTED",
    "pricingSummary": {
        "priceSubtotal": {"value": "173.47", "currency": "USD"},
        "deliveryCost": {"value": "0.0", "currency": "USD"},
        "total": {"value": "173.47", "currency": "USD"},
    },
    "lineItems": [
        {
            "lineItemId": "10085330527613",
            "legacyItemId": "366518074803",
            "sku": "FINANCE-TEST-SKU",
            "title": "Test Bed",
            "quantity": 1,
            "lineItemCost": {"value": "173.47", "currency": "USD"},
        }
    ],
}


def _seed_product(conn: sqlite3.Connection, sku: str = "FINANCE-TEST-SKU") -> dict:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collected_products (
            sku TEXT PRIMARY KEY,
            price REAL,
            shipping REAL,
            cost_breakdown TEXT
        )
        """
    )
    cost = PricingEngine.calculate_dajian_cost(100.0, 20.0)
    conn.execute(
        "INSERT OR REPLACE INTO collected_products (sku, price, shipping, cost_breakdown) VALUES (?,?,?,?)",
        (sku, 100.0, 20.0, json.dumps(cost)),
    )
    conn.commit()
    return cost


def test_estimate_line_fees_positive():
    fee = estimate_line_fees(100.0, ad_rate=0.05, order_line_count=1)
    assert fee > 10  # roughly 0.95*100*0.1825 + 0.30
    assert fee < 30


def test_upsert_finance_order_with_recomputed_cost(tmp_path):
    db = tmp_path / "f.db"
    conn = sqlite3.connect(str(db))
    cost = _seed_product(conn)

    summary = upsert_finance_order(conn, SAMPLE, ad_rate=0.05)
    assert summary["platform_order_id"] == "13-15010-93245"
    assert summary["gross_sales"] == 173.47
    assert summary["total_cogs"] == cost["total_dajian_cost"]
    assert summary["line_count"] == 1
    assert summary["lines"][0]["ebay_item_number"] == "366518074803"
    assert summary["lines"][0]["ebay_transaction_id"] == "10085330527613"

    # net = after-store-discount proceeds - cogs - fees
    proceeds = round(summary["gross_sales"] * 0.95, 2)
    assert abs(summary["net_est"] - (proceeds - summary["total_cogs"] - summary["total_fees_est"])) < 0.02

    s = query_finance_summary(conn)
    assert s["order_count"] == 1
    assert s["gmv"] == 173.47
    assert s["not_pushed"] == 1

    # idempotent upsert
    upsert_finance_order(conn, SAMPLE, ad_rate=0.05)
    s2 = query_finance_summary(conn)
    assert s2["order_count"] == 1
    conn.close()


def test_build_lines_missing_sku_cost(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "f.db"))
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT, price REAL, shipping REAL, cost_breakdown TEXT)"
    )
    conn.commit()
    lines = build_lines_from_ebay_order(conn, SAMPLE)
    assert lines[0].unit_giga_cost == 0.0
    assert lines[0].cost_source == "sku_not_in_db"
    proceeds = round(lines[0].line_gross * 0.95, 2)
    assert abs(lines[0].net_est - (proceeds - lines[0].line_cogs - lines[0].fee_est)) < 0.02
    conn.close()


def test_paid_order_cogs_stay_locked_after_catalog_reprice(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "f.db"))
    cost = _seed_product(conn)
    first = upsert_finance_order(conn, SAMPLE, ad_rate=0.05)
    locked = first["lines"][0]["unit_giga_cost"]
    assert locked == cost["total_dajian_cost"]

    conn.execute(
        "UPDATE collected_products SET price = 400, shipping = 80, cost_breakdown = ?",
        (json.dumps(PricingEngine.calculate_dajian_cost(400.0, 80.0)),),
    )
    conn.commit()

    second = upsert_finance_order(conn, SAMPLE, ad_rate=0.05)
    assert second["lines"][0]["unit_giga_cost"] == locked
    assert second["total_cogs"] == first["total_cogs"]
    conn.close()


def test_net_est_uses_after_store_discount_proceeds(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "f.db"))
    _seed_product(conn)
    summary = upsert_finance_order(conn, SAMPLE, ad_rate=0.05)
    line = summary["lines"][0]
    proceeds = round(line["line_gross"] * 0.95, 2)
    assert line["net_est"] < round(line["line_gross"] - line["unit_giga_cost"] - line["fee_est"], 2)
    assert abs(line["net_est"] - (proceeds - line["unit_giga_cost"] - line["fee_est"])) < 0.02
    conn.close()


def test_classify_giga_fulfill_stage():
    assert classify_giga_fulfill_stage(None, has_giga_row=False) == "not_pushed"
    assert classify_giga_fulfill_stage("pushed", has_giga_row=True) == "processing"
    assert classify_giga_fulfill_stage(
        "pushed", has_giga_row=True, giga_api_status="Unpaid"
    ) == "pushed_unpaid"
    assert classify_giga_fulfill_stage("shipped", has_giga_row=True) == "shipped"
    assert classify_giga_fulfill_stage("push_failed", has_giga_row=True) == "error"
    assert classify_giga_fulfill_stage("still_processing", has_giga_row=True) == "processing"
    # eBay already fulfilled, no GIGA row → treat as shipped (not 未推)
    assert (
        classify_giga_fulfill_stage(
            None,
            has_giga_row=False,
            ebay_fulfillment_status="FULFILLED",
        )
        == "shipped"
    )


def test_fulfill_stage_join_and_refresh_links(tmp_path):
    db = tmp_path / "f.db"
    conn = sqlite3.connect(str(db))
    _seed_product(conn)
    ensure_fulfillment_table(conn)

    upsert_finance_order(conn, SAMPLE, ad_rate=0.05)
    orders = query_finance_orders(conn)
    assert orders[0]["fulfill_stage"] == "not_pushed"

    record_fulfillment_attempt(
        conn,
        ebay_order_id="13-15010-93245",
        giga_order_no="13-15010-93245R2",
        status="pushed",
        payload={"ok": True},
    )
    # tracking with Unpaid → pushed_unpaid
    conn.execute(
        "UPDATE giga_fulfillment_orders SET tracking_json = ? WHERE ebay_order_id = ?",
        (json.dumps({"giga_status": "Unpaid"}), "13-15010-93245"),
    )
    conn.commit()

    rep = refresh_giga_links(conn)
    assert rep["updated"] >= 1

    orders2 = query_finance_orders(conn)
    assert orders2[0]["fulfill_stage"] == "pushed_unpaid"
    assert orders2[0]["giga_order_no_live"] == "13-15010-93245R2"
    assert orders2[0]["giga_order_no"] == "13-15010-93245R2"

    s = query_finance_summary(conn)
    assert s["pushed_unpaid"] == 1
    assert s["not_pushed"] == 0

    # filter
    only_np = query_finance_orders(conn, only_not_pushed=True)
    assert only_np == []
    only_unpaid = query_finance_orders(conn, fulfill_stage="pushed_unpaid")
    assert len(only_unpaid) == 1

    conn.execute(
        "UPDATE giga_fulfillment_orders SET status = 'shipped', tracking_json = ? WHERE ebay_order_id = ?",
        (json.dumps({"giga_status": "Shipped"}), "13-15010-93245"),
    )
    conn.commit()
    assert query_finance_orders(conn)[0]["fulfill_stage"] == "shipped"
    conn.close()


def test_render_finance_email_html(tmp_path):
    db = tmp_path / "f.db"
    conn = sqlite3.connect(str(db))
    _seed_product(conn)
    # force a loss: high cogs via expensive product
    conn.execute(
        "UPDATE collected_products SET price = 200, shipping = 50, cost_breakdown = ?",
        (json.dumps(PricingEngine.calculate_dajian_cost(200.0, 50.0)),),
    )
    conn.commit()
    upsert_finance_order(conn, SAMPLE, ad_rate=0.05)
    html = render_finance_email_html(conn, range_key="all")
    assert "财务摘要" in html
    assert "GMV" in html
    assert "13-15010-93245" in html or "PAID 订单" in html
    assert "估" in html
    assert "利润率" in html
    conn.close()

    # missing db path
    html_missing = render_finance_email_html(db_path=tmp_path / "nope.db")
    assert "财务摘要" in html_missing


def test_resolve_finance_date_range():
    now = datetime(2026, 8, 12, 15, 0, 0)
    today = resolve_finance_date_range("today", now=now)
    assert today["date_from"] == "2026-08-12"
    assert today["date_to"] == "2026-08-12"
    assert today["label"] == "今日"

    week = resolve_finance_date_range("7d", now=now)
    assert week["date_from"] == "2026-08-06"
    assert week["date_to"] == "2026-08-12"

    mtd = resolve_finance_date_range("mtd", now=now)
    assert mtd["date_from"] == "2026-08-01"
    assert mtd["date_to"] == "2026-08-12"

    all_r = resolve_finance_date_range("all", now=now)
    assert all_r["date_from"] is None
    assert all_r["date_to"] is None


def test_summary_excludes_refunds_and_respects_date_range(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "f.db"))
    _seed_product(conn)

    paid = dict(SAMPLE)
    upsert_finance_order(conn, paid, ad_rate=0.05)

    refunded = dict(SAMPLE)
    refunded["orderId"] = "04-15027-11320"
    refunded["orderPaymentStatus"] = "FULLY_REFUNDED"
    refunded["creationDate"] = "2026-08-10T12:00:00.000Z"
    refunded["lineItems"] = [
        {
            **SAMPLE["lineItems"][0],
            "lineItemId": "999",
            "legacyItemId": "111",
            "sku": "FINANCE-TEST-SKU",
            "lineItemCost": {"value": "62.41", "currency": "USD"},
        }
    ]
    refunded["pricingSummary"] = {
        "priceSubtotal": {"value": "62.41", "currency": "USD"},
        "deliveryCost": {"value": "0", "currency": "USD"},
        "total": {"value": "62.41", "currency": "USD"},
    }
    upsert_finance_order(conn, refunded, ad_rate=0.05)

    # old paid outside range
    old = dict(SAMPLE)
    old["orderId"] = "01-old-order"
    old["creationDate"] = "2026-07-01T10:00:00.000Z"
    upsert_finance_order(conn, old, ad_rate=0.05)

    now = datetime(2026, 8, 12, 12, 0, 0)
    s_all = query_finance_summary(conn, range_key="all", now=now)
    assert s_all["order_count"] == 2  # two PAID only
    assert s_all["refund_orders"] == 1
    assert abs(s_all["refund_gmv"] - 62.41) < 0.01
    assert s_all["gmv"] == 173.47 * 2
    assert "margin" in s_all

    s_7d = query_finance_summary(conn, range_key="7d", now=now)
    assert s_7d["order_count"] == 1  # only Aug 11 paid
    assert abs(s_7d["gmv"] - 173.47) < 0.01
    assert s_7d["refund_orders"] == 1

    s_today = query_finance_summary(conn, range_key="today", now=now)
    assert s_today["order_count"] == 0

    refunds = query_finance_orders(conn, include_refunds_only=True, range_key="all")
    assert len(refunds) == 1
    assert refunds[0]["payment_status"] == "FULLY_REFUNDED"

    paid_only = query_finance_orders(
        conn, payment_status="PAID", range_key="7d", now=now
    )
    assert len(paid_only) == 1
    conn.close()
