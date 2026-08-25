"""Finance F0: order-level PnL from eBay sales + GIGA cost snapshots.

COGS basis: PricingEngine.calculate_dajian_cost (GIGA order product+shipping+insurance+alipay).
Fees: estimated eBay FVF + ad + fixed fee (store discount model).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "ebay_collection.db"

_Q2 = Decimal("0.01")
DEFAULT_STORE_DISCOUNT = 0.05


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def _q2(value: Decimal) -> float:
    return float(value.quantize(_Q2, rounding=ROUND_HALF_UP))


def _after_store_discount(
    gross: float,
    store_discount: float = DEFAULT_STORE_DISCOUNT,
) -> Decimal:
    return _d(gross) * (Decimal("1") - _d(store_discount))


# ─── Schema ────────────────────────────────────────────────────────────────

def ensure_finance_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS finance_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL DEFAULT 'ebay',
            platform_order_id TEXT NOT NULL,
            order_date TEXT,
            paid_date TEXT,
            currency TEXT DEFAULT 'USD',
            payment_status TEXT,
            fulfillment_status TEXT,
            gross_sales REAL DEFAULT 0,
            shipping_charged REAL DEFAULT 0,
            tax_collected REAL DEFAULT 0,
            order_total REAL DEFAULT 0,
            total_cogs REAL DEFAULT 0,
            total_fees_est REAL DEFAULT 0,
            net_est REAL DEFAULT 0,
            margin_est REAL DEFAULT 0,
            giga_order_no TEXT,
            pnl_status TEXT DEFAULT 'estimated',
            raw_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(platform, platform_order_id)
        );

        CREATE TABLE IF NOT EXISTS finance_order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            finance_order_id INTEGER NOT NULL,
            platform_order_id TEXT NOT NULL,
            line_item_id TEXT,
            sku TEXT,
            title TEXT,
            ebay_item_number TEXT,
            ebay_transaction_id TEXT,
            qty INTEGER DEFAULT 1,
            unit_sell_price REAL DEFAULT 0,
            line_gross REAL DEFAULT 0,
            unit_giga_cost REAL DEFAULT 0,
            line_cogs REAL DEFAULT 0,
            cost_source TEXT,
            fee_est REAL DEFAULT 0,
            net_est REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (finance_order_id) REFERENCES finance_orders(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_finance_orders_date
            ON finance_orders(order_date);
        CREATE INDEX IF NOT EXISTS idx_finance_lines_sku
            ON finance_order_lines(sku);
        CREATE INDEX IF NOT EXISTS idx_finance_lines_order
            ON finance_order_lines(platform_order_id);
        """
    )
    conn.commit()


# ─── Cost / fee math ───────────────────────────────────────────────────────

@dataclass
class LinePnl:
    sku: str
    title: str
    line_item_id: str
    ebay_item_number: str
    ebay_transaction_id: str
    qty: int
    unit_sell_price: float
    line_gross: float
    unit_giga_cost: float
    line_cogs: float
    cost_source: str
    fee_est: float
    net_est: float


def estimate_line_fees(
    line_gross: float,
    *,
    ad_rate: float = 0.05,
    store_discount: float = DEFAULT_STORE_DISCOUNT,
    fvf: float = 0.1325,
    fixed_fee: float = 0.30,
    order_line_count: int = 1,
) -> float:
    """Estimate platform fees for one line (share of fixed fee across lines)."""
    gross = _d(line_gross)
    if gross <= 0:
        return 0.0
    # After store discount (buyer pays less → FVF base lower)
    after_discount = _after_store_discount(float(gross), store_discount)
    variable = after_discount * (_d(fvf) + _d(ad_rate))
    n = max(int(order_line_count or 1), 1)
    fixed_share = _d(fixed_fee) / Decimal(n)
    return _q2(variable + fixed_share)


def lookup_giga_unit_cost(conn: sqlite3.Connection, sku: str) -> tuple[float, str]:
    """Return (unit_giga_cost, source) from collected_products."""
    from src.services.pricing_engine import PricingEngine

    row = conn.execute(
        "SELECT price, shipping, cost_breakdown FROM collected_products WHERE sku = ?",
        (sku,),
    ).fetchone()
    if not row:
        return 0.0, "sku_not_in_db"

    price, shipping, cb_raw = row[0], row[1], row[2]
    cb = {}
    if cb_raw:
        try:
            cb = json.loads(cb_raw) if isinstance(cb_raw, str) else (cb_raw or {})
        except Exception:
            cb = {}

    stored = cb.get("total_dajian_cost")
    try:
        stored_f = float(stored or 0)
    except (TypeError, ValueError):
        stored_f = 0.0
    if stored_f > 0:
        return stored_f, "cost_breakdown.total_dajian_cost"

    try:
        pp = float(price or 0)
        sh = float(shipping or 0)
    except (TypeError, ValueError):
        return 0.0, "missing_giga_price"

    if pp <= 0:
        return 0.0, "missing_giga_price"

    cost = PricingEngine.calculate_dajian_cost(pp, sh)
    return float(cost["total_dajian_cost"]), "recomputed_from_giga_order"


def load_locked_unit_costs(
    conn: sqlite3.Connection,
    platform_order_id: str,
) -> dict[str, tuple[float, str]]:
    """Return paid-time COGS snapshots keyed by line_item_id and sku.

    Positive ``unit_giga_cost`` values are frozen: later catalog refreshes
    must not rewrite historical PnL. Zero/missing costs are omitted so a
    later sync can fill them in once.
    """
    locked: dict[str, tuple[float, str]] = {}
    try:
        rows = conn.execute(
            """
            SELECT line_item_id, sku, unit_giga_cost, cost_source
            FROM finance_order_lines
            WHERE platform_order_id = ?
            """,
            (platform_order_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return locked

    for line_item_id, sku, unit_cost, source in rows:
        try:
            cost_f = float(unit_cost or 0)
        except (TypeError, ValueError):
            continue
        if cost_f <= 0:
            continue
        source_s = str(source or "locked_snapshot")
        if line_item_id:
            locked[str(line_item_id)] = (cost_f, source_s)
        if sku:
            locked[f"sku:{sku}"] = (cost_f, source_s)
    return locked


def build_lines_from_ebay_order(
    conn: sqlite3.Connection,
    order: dict,
    *,
    ad_rate: float = 0.05,
    store_discount: float = DEFAULT_STORE_DISCOUNT,
    locked_costs: Optional[dict[str, tuple[float, str]]] = None,
) -> list[LinePnl]:
    raw_lines = [li for li in (order.get("lineItems") or []) if li.get("sku")]
    n = max(len(raw_lines), 1)
    out: list[LinePnl] = []
    locked = locked_costs or {}

    for li in raw_lines:
        sku = str(li.get("sku") or "").strip()
        qty = int(li.get("quantity") or 0) or 1
        line_gross = float((li.get("lineItemCost") or {}).get("value") or 0)
        if line_gross <= 0:
            line_gross = float((li.get("total") or {}).get("value") or 0)
        unit_sell = round(line_gross / qty, 2) if qty else line_gross
        line_id = str(li.get("lineItemId") or "")
        if line_id and line_id in locked:
            unit_cost, cost_source = locked[line_id]
        elif f"sku:{sku}" in locked:
            unit_cost, cost_source = locked[f"sku:{sku}"]
        else:
            unit_cost, cost_source = lookup_giga_unit_cost(conn, sku)
        line_cogs = round(unit_cost * qty, 2)
        fee = estimate_line_fees(
            line_gross,
            ad_rate=ad_rate,
            store_discount=store_discount,
            order_line_count=n,
        )
        proceeds = _q2(_after_store_discount(line_gross, store_discount))
        net = round(proceeds - line_cogs - fee, 2)
        out.append(
            LinePnl(
                sku=sku,
                title=str(li.get("title") or "")[:200],
                line_item_id=str(li.get("lineItemId") or ""),
                ebay_item_number=str(li.get("legacyItemId") or ""),
                ebay_transaction_id=str(li.get("lineItemId") or ""),
                qty=qty,
                unit_sell_price=unit_sell,
                line_gross=line_gross,
                unit_giga_cost=unit_cost,
                line_cogs=line_cogs,
                cost_source=cost_source,
                fee_est=fee,
                net_est=net,
            )
        )
    return out


def upsert_finance_order(
    conn: sqlite3.Connection,
    order: dict,
    *,
    ad_rate: float = 0.05,
    giga_order_no: Optional[str] = None,
) -> dict:
    """Insert/update one eBay order into finance tables. Returns summary dict."""
    ensure_finance_tables(conn)
    platform_order_id = str(order.get("orderId") or order.get("legacyOrderId") or "").strip()
    if not platform_order_id:
        raise ValueError("order missing orderId")

    created = str(order.get("creationDate") or "")
    order_date = created.replace("T", " ").replace("Z", "")
    if "." in order_date:
        order_date = order_date.split(".")[0]

    pricing = order.get("pricingSummary") or {}
    gross = float((pricing.get("priceSubtotal") or {}).get("value") or 0)
    ship = float((pricing.get("deliveryCost") or {}).get("value") or 0)
    total = float((pricing.get("total") or {}).get("value") or 0)
    # tax often in ebayCollectAndRemitTaxes on lines — approximate from total-gross-ship
    tax = 0.0
    for li in order.get("lineItems") or []:
        for t in li.get("ebayCollectAndRemitTaxes") or []:
            tax += float((t.get("amount") or {}).get("value") or 0)

    locked_costs = load_locked_unit_costs(conn, platform_order_id)
    lines = build_lines_from_ebay_order(
        conn,
        order,
        ad_rate=ad_rate,
        locked_costs=locked_costs,
    )
    if gross <= 0:
        gross = sum(l.line_gross for l in lines)
    if total <= 0:
        total = gross + ship

    total_cogs = round(sum(l.line_cogs for l in lines), 2)
    total_fees = round(sum(l.fee_est for l in lines), 2)
    # Net on after-discount proceeds (tax not seller revenue)
    if lines:
        net_est = round(sum(l.net_est for l in lines), 2)
    else:
        proceeds = _q2(_after_store_discount(gross))
        net_est = round(proceeds - total_cogs - total_fees, 2)
    margin = round(net_est / gross, 4) if gross > 0 else 0.0

    # Link GIGA fulfillment if present
    if not giga_order_no:
        try:
            row = conn.execute(
                "SELECT giga_order_no FROM giga_fulfillment_orders WHERE ebay_order_id = ?",
                (platform_order_id,),
            ).fetchone()
            if row:
                giga_order_no = row[0]
        except Exception:
            pass

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    conn.execute(
        """
        INSERT INTO finance_orders (
            platform, platform_order_id, order_date, paid_date, currency,
            payment_status, fulfillment_status,
            gross_sales, shipping_charged, tax_collected, order_total,
            total_cogs, total_fees_est, net_est, margin_est,
            giga_order_no, pnl_status, raw_json, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(platform, platform_order_id) DO UPDATE SET
            order_date=excluded.order_date,
            payment_status=excluded.payment_status,
            fulfillment_status=excluded.fulfillment_status,
            gross_sales=excluded.gross_sales,
            shipping_charged=excluded.shipping_charged,
            tax_collected=excluded.tax_collected,
            order_total=excluded.order_total,
            total_cogs=excluded.total_cogs,
            total_fees_est=excluded.total_fees_est,
            net_est=excluded.net_est,
            margin_est=excluded.margin_est,
            giga_order_no=COALESCE(excluded.giga_order_no, finance_orders.giga_order_no),
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        (
            "ebay",
            platform_order_id,
            order_date or None,
            order_date if str(order.get("orderPaymentStatus") or "").upper() == "PAID" else None,
            "USD",
            order.get("orderPaymentStatus"),
            order.get("orderFulfillmentStatus"),
            gross,
            ship,
            tax,
            total,
            total_cogs,
            total_fees,
            net_est,
            margin,
            giga_order_no,
            "estimated",
            json.dumps(
                {
                    "orderId": platform_order_id,
                    "line_count": len(lines),
                },
                ensure_ascii=False,
            ),
            now,
        ),
    )

    # Replace lines for this order
    fid = conn.execute(
        "SELECT id FROM finance_orders WHERE platform='ebay' AND platform_order_id=?",
        (platform_order_id,),
    ).fetchone()[0]
    conn.execute("DELETE FROM finance_order_lines WHERE finance_order_id = ?", (fid,))
    for ln in lines:
        conn.execute(
            """
            INSERT INTO finance_order_lines (
                finance_order_id, platform_order_id, line_item_id, sku, title,
                ebay_item_number, ebay_transaction_id, qty,
                unit_sell_price, line_gross, unit_giga_cost, line_cogs,
                cost_source, fee_est, net_est
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                fid,
                platform_order_id,
                ln.line_item_id,
                ln.sku,
                ln.title,
                ln.ebay_item_number,
                ln.ebay_transaction_id,
                ln.qty,
                ln.unit_sell_price,
                ln.line_gross,
                ln.unit_giga_cost,
                ln.line_cogs,
                ln.cost_source,
                ln.fee_est,
                ln.net_est,
            ),
        )
    conn.commit()

    return {
        "platform_order_id": platform_order_id,
        "gross_sales": gross,
        "total_cogs": total_cogs,
        "total_fees_est": total_fees,
        "net_est": net_est,
        "margin_est": margin,
        "line_count": len(lines),
        "giga_order_no": giga_order_no,
        "payment_status": order.get("orderPaymentStatus"),
        "fulfillment_status": order.get("orderFulfillmentStatus"),
        "lines": [
            {
                "sku": ln.sku,
                "ebay_item_number": ln.ebay_item_number,
                "ebay_transaction_id": ln.ebay_transaction_id,
                "line_gross": ln.line_gross,
                "unit_giga_cost": ln.unit_giga_cost,
                "fee_est": ln.fee_est,
                "net_est": ln.net_est,
                "cost_source": ln.cost_source,
            }
            for ln in lines
        ],
    }


def sync_orders_from_ebay(
    *,
    days: int = 30,
    db_path: str | Path = DEFAULT_DB,
    ad_rate: float = 0.05,
    oauth=None,
) -> dict:
    """Pull eBay orders and upsert finance PnL."""
    from src.services.giga_dropship import fetch_ebay_orders

    conn = sqlite3.connect(str(db_path))
    ensure_finance_tables(conn)
    orders = fetch_ebay_orders(days=days, oauth=oauth)
    results = []
    for order in orders:
        # Finance includes all paid orders (even fulfilled)
        pay = str(order.get("orderPaymentStatus") or "").upper()
        if pay not in ("PAID", "PARTIALLY_REFUNDED", "FULLY_REFUNDED"):
            # still record unpaid? skip unpaid for F0
            if pay and pay != "PAID":
                continue
            if not pay:
                continue
        try:
            results.append(upsert_finance_order(conn, order, ad_rate=ad_rate))
        except Exception as e:
            logger.warning("finance upsert failed: %s", e)
            results.append(
                {
                    "platform_order_id": order.get("orderId"),
                    "error": str(e),
                }
            )
    link_report = refresh_giga_links(conn)
    conn.close()
    return {
        "orders_pulled": len(orders),
        "upserted": sum(1 for r in results if not r.get("error")),
        "errors": sum(1 for r in results if r.get("error")),
        "giga_links_updated": link_report.get("updated", 0),
        "results": results,
    }


# ─── F1: GIGA fulfillment stage ───────────────────────────────────────────

# Local giga_fulfillment_orders.status → operator-facing stage
FULFILL_STAGE_LABELS_ZH = {
    "not_pushed": "未推 GIGA",
    "pushed_unpaid": "已推未付",
    "processing": "处理中",
    "shipped": "已发货",
    "error": "异常",
    "unknown": "未知",
}


def classify_giga_fulfill_stage(
    giga_local_status: Optional[str],
    *,
    has_giga_row: bool,
    giga_api_status: Optional[str] = None,
    ebay_fulfillment_status: Optional[str] = None,
) -> str:
    """Map local fulfillment row (+ optional GIGA API orderStatus) to a stage key.

    eBay FULFILLED without a GIGA row still counts as shipped (manual/legacy fulfill),
    so finance does not alarm '未推 GIGA' for already-done orders.
    """
    ebay_ful = str(ebay_fulfillment_status or "").strip().upper()
    local = str(giga_local_status or "").strip().lower()
    api = str(giga_api_status or "").strip().lower()
    combined = f"{local} {api}"

    if local in ("shipped",) or "shipped" in api or api in ("delivered", "completed"):
        return "shipped"
    if ebay_ful in ("FULFILLED", "FULFILLED_PARTIALLY") and (
        not has_giga_row or local in ("", "dry_run_ready")
    ):
        # Already closed on eBay; GIGA push not required for ops board
        return "shipped"
    if not has_giga_row:
        return "not_pushed"
    if local in (
        "push_failed",
        "push_unknown",
        "manual_review",
        "fulfill_failed",
        "sync_error",
        "error",
    ) or "fail" in local:
        return "error"
    if "unpaid" in combined or "未付" in combined:
        return "pushed_unpaid"
    if local in ("pushed", "still_processing", "dry_run_ready", "push_ok"):
        return "processing"
    if local:
        return "unknown"
    return "not_pushed"


def _giga_api_status_from_tracking(tracking_json: Any) -> Optional[str]:
    if not tracking_json:
        return None
    try:
        data = json.loads(tracking_json) if isinstance(tracking_json, str) else tracking_json
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("giga_status", "orderStatus", "order_status", "status"):
        val = data.get(key)
        if val:
            return str(val)
    return None


def refresh_giga_links(conn: sqlite3.Connection) -> dict:
    """Backfill finance_orders.giga_order_no from giga_fulfillment_orders."""
    ensure_finance_tables(conn)
    try:
        rows = conn.execute(
            """
            SELECT fo.platform_order_id, g.giga_order_no
            FROM finance_orders fo
            JOIN giga_fulfillment_orders g ON g.ebay_order_id = fo.platform_order_id
            WHERE fo.giga_order_no IS NULL OR fo.giga_order_no = ''
               OR fo.giga_order_no != g.giga_order_no
            """
        ).fetchall()
    except sqlite3.OperationalError:
        # Table may not exist yet in fresh DBs
        return {"updated": 0, "note": "giga_fulfillment_orders missing"}

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    updated = 0
    for platform_order_id, giga_order_no in rows:
        conn.execute(
            """
            UPDATE finance_orders
            SET giga_order_no = ?, updated_at = ?
            WHERE platform = 'ebay' AND platform_order_id = ?
            """,
            (giga_order_no, now, platform_order_id),
        )
        updated += 1
    if updated:
        conn.commit()
    return {"updated": updated}


def attach_fulfill_stage(order_row: dict) -> dict:
    """Mutate/return a finance order dict with fulfill_stage (+ label)."""
    giga_no_live = order_row.get("giga_order_no_live")
    local_status = order_row.get("giga_local_status")
    # LEFT JOIN hit: local status or live giga order no present
    has_row = local_status is not None or bool(giga_no_live)
    api_status = order_row.get("giga_api_status")
    stage = classify_giga_fulfill_stage(
        local_status,
        has_giga_row=has_row,
        giga_api_status=api_status,
        ebay_fulfillment_status=order_row.get("fulfillment_status"),
    )
    order_row["fulfill_stage"] = stage
    order_row["fulfill_stage_label"] = FULFILL_STAGE_LABELS_ZH.get(stage, stage)
    if giga_no_live and not order_row.get("giga_order_no"):
        order_row["giga_order_no"] = giga_no_live
    return order_row


# Date range presets for dashboard / email
FINANCE_RANGE_KEYS = ("today", "7d", "30d", "mtd", "all")
FINANCE_RANGE_LABELS_ZH = {
    "today": "今日",
    "7d": "近7天",
    "30d": "近30天",
    "mtd": "本月",
    "all": "全部",
}


def resolve_finance_date_range(
    range_key: str = "30d",
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Return inclusive calendar-day bounds for finance filters.

    Keys: today | 7d | 30d | mtd | all
    date_from / date_to are YYYY-MM-DD strings (inclusive), or None for open end.
    """
    ts = now or datetime.now()
    today = ts.date() if isinstance(ts, datetime) else ts
    if not isinstance(today, date):
        today = datetime.now().date()

    key = str(range_key or "30d").strip().lower()
    aliases = {
        "日": "today",
        "今日": "today",
        "day": "today",
        "week": "7d",
        "近7天": "7d",
        "month": "mtd",
        "本月": "mtd",
        "近30天": "30d",
        "全部": "all",
    }
    key = aliases.get(key, key)
    if key not in FINANCE_RANGE_KEYS:
        key = "30d"

    start: Optional[date]
    end: Optional[date] = today
    if key == "today":
        start = today
    elif key == "7d":
        start = today - timedelta(days=6)
    elif key == "30d":
        start = today - timedelta(days=29)
    elif key == "mtd":
        start = today.replace(day=1)
    else:  # all
        start, end = None, None

    return {
        "range_key": key,
        "label": FINANCE_RANGE_LABELS_ZH.get(key, key),
        "date_from": start.isoformat() if start else None,
        "date_to": end.isoformat() if end else None,
    }


def _sql_order_date_clause(
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    column: str = "order_date",
) -> tuple[str, list[Any]]:
    """Filter by calendar day of order_date (stored as 'YYYY-MM-DD ...' text)."""
    parts: list[str] = []
    params: list[Any] = []
    day_expr = f"substr(COALESCE({column}, ''), 1, 10)"
    if date_from:
        parts.append(f"{day_expr} >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        parts.append(f"{day_expr} <= ?")
        params.append(str(date_to)[:10])
    if not parts:
        return "", []
    return " AND " + " AND ".join(parts), params


def query_finance_summary(
    conn: sqlite3.Connection,
    *,
    range_key: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """PAID-only P&L summary. Refunds are reported separately and excluded from GMV/net.

    Pass range_key (today/7d/30d/mtd/all) or explicit date_from/date_to (YYYY-MM-DD).
    Explicit dates override range_key.
    """
    ensure_finance_tables(conn)
    if date_from is None and date_to is None and range_key:
        bounds = resolve_finance_date_range(range_key, now=now)
        date_from = bounds["date_from"]
        date_to = bounds["date_to"]
        range_meta = bounds
    else:
        range_meta = {
            "range_key": range_key or "custom",
            "label": FINANCE_RANGE_LABELS_ZH.get(range_key or "", range_key or "自定义"),
            "date_from": date_from,
            "date_to": date_to,
        }

    date_sql, date_params = _sql_order_date_clause(date_from=date_from, date_to=date_to)
    date_sql_fo, date_params_fo = _sql_order_date_clause(
        date_from=date_from, date_to=date_to, column="fo.order_date"
    )

    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS order_count,
            COALESCE(SUM(gross_sales), 0) AS gmv,
            COALESCE(SUM(total_cogs), 0) AS cogs,
            COALESCE(SUM(total_fees_est), 0) AS fees,
            COALESCE(SUM(net_est), 0) AS net,
            SUM(CASE WHEN net_est < 0 THEN 1 ELSE 0 END) AS loss_orders
        FROM finance_orders
        WHERE payment_status = 'PAID'
        {date_sql}
        """,
        date_params,
    ).fetchone()
    gmv = float(row[1] or 0)
    net = float(row[4] or 0)

    # Refunds: in table but never in PAID GMV/net (read-only ops signal)
    refund_row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS refund_orders,
            COALESCE(SUM(gross_sales), 0) AS refund_gmv
        FROM finance_orders
        WHERE payment_status IN ('FULLY_REFUNDED', 'PARTIALLY_REFUNDED')
        {date_sql}
        """,
        date_params,
    ).fetchone()
    refund_orders = int(refund_row[0] or 0)
    refund_gmv = float(refund_row[1] or 0)

    # Fulfillment stage counts (best-effort join) — PAID only
    stage_counts = {
        "not_pushed": 0,
        "pushed_unpaid": 0,
        "processing": 0,
        "shipped": 0,
        "error": 0,
        "unknown": 0,
    }
    try:
        join_rows = conn.execute(
            f"""
            SELECT fo.platform_order_id, fo.giga_order_no, fo.fulfillment_status,
                   g.status, g.tracking_json, g.giga_order_no
            FROM finance_orders fo
            LEFT JOIN giga_fulfillment_orders g ON g.ebay_order_id = fo.platform_order_id
            WHERE fo.payment_status = 'PAID'
            {date_sql_fo}
            """,
            date_params_fo,
        ).fetchall()
        for _oid, fo_giga, ebay_ful, g_status, tracking_json, g_giga in join_rows:
            has_row = g_status is not None or g_giga is not None
            api = _giga_api_status_from_tracking(tracking_json)
            stage = classify_giga_fulfill_stage(
                g_status,
                has_giga_row=has_row,
                giga_api_status=api,
                ebay_fulfillment_status=ebay_ful,
            )
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
    except sqlite3.OperationalError:
        # No giga table: all PAID count as not_pushed
        stage_counts["not_pushed"] = int(row[0] or 0)

    return {
        "order_count": int(row[0] or 0),
        "gmv": gmv,
        "cogs": float(row[2] or 0),
        "fees": float(row[3] or 0),
        "net": net,
        "margin": (net / gmv) if gmv > 0 else 0.0,
        "loss_orders": int(row[5] or 0),
        "fulfill_stages": stage_counts,
        "not_pushed": stage_counts.get("not_pushed", 0),
        "pushed_unpaid": stage_counts.get("pushed_unpaid", 0),
        "shipped": stage_counts.get("shipped", 0),
        "refund_orders": refund_orders,
        "refund_gmv": refund_gmv,
        "range": range_meta,
        "date_from": date_from,
        "date_to": date_to,
    }


def query_finance_orders(
    conn: sqlite3.Connection,
    *,
    only_loss: bool = False,
    only_not_pushed: bool = False,
    fulfill_stage: Optional[str] = None,
    range_key: str = "all",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    payment_status: Optional[str] = None,
    include_refunds_only: bool = False,
    now: Optional[datetime] = None,
    limit: int = 200,
) -> list[dict]:
    ensure_finance_tables(conn)
    if date_from is None and date_to is None and range_key and range_key != "all":
        bounds = resolve_finance_date_range(range_key, now=now)
        date_from = bounds["date_from"]
        date_to = bounds["date_to"]
    elif date_from is None and date_to is None and range_key == "all":
        date_from = date_to = None

    date_sql, date_params = _sql_order_date_clause(
        date_from=date_from, date_to=date_to, column="fo.order_date"
    )
    date_sql_plain, date_params_plain = _sql_order_date_clause(
        date_from=date_from, date_to=date_to, column="order_date"
    )

    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT fo.*,
                   g.status AS giga_local_status,
                   g.giga_order_no AS giga_order_no_live,
                   g.tracking_json AS giga_tracking_json,
                   g.last_error AS giga_last_error
            FROM finance_orders fo
            LEFT JOIN giga_fulfillment_orders g ON g.ebay_order_id = fo.platform_order_id
            WHERE 1=1
        """
        params: list[Any] = []
        if include_refunds_only:
            sql += " AND fo.payment_status IN ('FULLY_REFUNDED', 'PARTIALLY_REFUNDED')"
        elif payment_status:
            sql += " AND fo.payment_status = ?"
            params.append(payment_status)
        if only_loss:
            sql += " AND fo.net_est < 0"
        sql += date_sql
        params.extend(date_params)
        sql += " ORDER BY fo.order_date DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.OperationalError:
        # giga table missing
        sql = "SELECT * FROM finance_orders WHERE 1=1"
        params = []
        if include_refunds_only:
            sql += " AND payment_status IN ('FULLY_REFUNDED', 'PARTIALLY_REFUNDED')"
        elif payment_status:
            sql += " AND payment_status = ?"
            params.append(payment_status)
        if only_loss:
            sql += " AND net_est < 0"
        sql += date_sql_plain
        params.extend(date_params_plain)
        sql += " ORDER BY order_date DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            r["giga_local_status"] = None
            r["giga_order_no_live"] = None
            r["giga_tracking_json"] = None

    out = []
    for r in rows:
        r["giga_api_status"] = _giga_api_status_from_tracking(r.get("giga_tracking_json"))
        attach_fulfill_stage(r)
        if only_not_pushed and r.get("fulfill_stage") != "not_pushed":
            continue
        if fulfill_stage and r.get("fulfill_stage") != fulfill_stage:
            continue
        out.append(r)
    return out


def query_finance_lines_for_order(conn: sqlite3.Connection, platform_order_id: str) -> list[dict]:
    ensure_finance_tables(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM finance_order_lines WHERE platform_order_id = ? ORDER BY id",
        (platform_order_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def render_finance_email_html(
    conn: Optional[sqlite3.Connection] = None,
    *,
    db_path: str | Path = DEFAULT_DB,
    range_key: str = "30d",
    max_loss_rows: int = 8,
    max_not_pushed: int = 8,
) -> str:
    """HTML fragment for daily summary email (Chinese, labeled 估)."""
    own_conn = conn is None
    if own_conn:
        path = Path(db_path)
        if not path.exists():
            return (
                '<h3>💰 财务摘要</h3>'
                f'<p style="color:#999;">数据库不存在: {path.name}</p>'
            )
        conn = sqlite3.connect(str(path))
    try:
        ensure_finance_tables(conn)
        refresh_giga_links(conn)
        summary = query_finance_summary(conn, range_key=range_key)
        range_label = (summary.get("range") or {}).get("label") or range_key
        if summary["order_count"] <= 0 and not summary.get("refund_orders"):
            return (
                f'<h3>💰 财务摘要（{ _esc(range_label) }）</h3>'
                '<p style="color:#999;">暂无已同步的 PAID 订单。可运行 '
                '<code>python scripts/finance_sync_orders.py --days 30</code>。</p>'
            )

        margin_pct = summary["margin"] * 100
        net_color = "#067647" if summary["net"] >= 0 else "#b42318"
        loss_n = summary["loss_orders"]
        not_pushed = summary.get("not_pushed", 0)
        pushed_unpaid = summary.get("pushed_unpaid", 0)
        shipped = summary.get("shipped", 0)
        stages = summary.get("fulfill_stages") or {}
        processing = stages.get("processing", 0)
        errors = stages.get("error", 0)
        refund_n = summary.get("refund_orders", 0)
        refund_gmv = summary.get("refund_gmv", 0.0)

        alert_bits = []
        if loss_n:
            alert_bits.append(
                f'<span style="color:#b42318;font-weight:bold;">💣 亏损单 {loss_n}</span>'
            )
        if not_pushed:
            alert_bits.append(
                f'<span style="color:#b54708;font-weight:bold;">📦 未推 GIGA {not_pushed}</span>'
            )
        if pushed_unpaid:
            alert_bits.append(
                f'<span style="color:#b54708;">💳 已推未付 {pushed_unpaid}</span>'
            )
        if errors:
            alert_bits.append(
                f'<span style="color:#b42318;">⚠️ 履约异常 {errors}</span>'
            )
        if refund_n:
            alert_bits.append(
                f'<span style="color:#667085;">↩ 退款单 {refund_n}（原 GMV ${refund_gmv:.2f}，不计入上表利润）</span>'
            )
        alert_html = (
            f'<p style="margin:8px 0;">{ " · ".join(alert_bits)}</p>' if alert_bits else ""
        )

        # Loss table
        loss_orders = query_finance_orders(
            conn, only_loss=True, range_key=range_key, payment_status="PAID", limit=max_loss_rows
        )
        if loss_orders:
            loss_rows = ""
            for o in loss_orders:
                loss_rows += (
                    f'<tr>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;">{_esc(o.get("platform_order_id"))}</td>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;">{_esc((o.get("order_date") or "")[:10])}</td>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">${float(o.get("gross_sales") or 0):.2f}</td>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">${float(o.get("total_cogs") or 0):.2f}</td>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;text-align:right;color:#b42318;font-weight:bold;">'
                    f'${float(o.get("net_est") or 0):.2f}</td>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;">{_esc(o.get("fulfill_stage_label") or "")}</td>'
                    f'</tr>'
                )
            loss_table = f'''
            <details open><summary>💣 预估亏损订单（最多 {max_loss_rows}）</summary>
            <table style="border-collapse:collapse;width:100%;margin-top:6px;font-size:12px;">
              <tr style="background:#fef3f2;">
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:left;">订单</th>
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:left;">日期</th>
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:right;">GMV</th>
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:right;">COGS</th>
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:right;">净利(估)</th>
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:left;">履约</th>
              </tr>{loss_rows}
            </table></details>'''
        else:
            loss_table = '<p style="color:#067647;font-size:13px;">✅ 当前无预估亏损订单</p>'

        not_pushed_orders = query_finance_orders(
            conn,
            only_not_pushed=True,
            range_key=range_key,
            payment_status="PAID",
            limit=max_not_pushed,
        )
        if not_pushed_orders:
            np_rows = ""
            for o in not_pushed_orders:
                np_rows += (
                    f'<tr>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;">{_esc(o.get("platform_order_id"))}</td>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;">{_esc((o.get("order_date") or "")[:10])}</td>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;text-align:right;">${float(o.get("gross_sales") or 0):.2f}</td>'
                    f'<td style="padding:5px 8px;border:1px solid #ddd;">{_esc(o.get("fulfillment_status") or "")}</td>'
                    f'</tr>'
                )
            not_pushed_table = f'''
            <details><summary>📦 未推 GIGA（最多 {max_not_pushed}）</summary>
            <table style="border-collapse:collapse;width:100%;margin-top:6px;font-size:12px;">
              <tr style="background:#fff7ed;">
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:left;">订单</th>
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:left;">日期</th>
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:right;">GMV</th>
                <th style="padding:5px 8px;border:1px solid #ddd;text-align:left;">eBay 履约</th>
              </tr>{np_rows}
            </table>
            <p style="color:#667085;font-size:11px;margin:6px 0 0;">推单: <code>python scripts/giga_dropship_push.py --order &lt;id&gt;</code></p>
            </details>'''
        else:
            not_pushed_table = ""

        return f'''
        <h3>💰 财务摘要（{_esc(range_label)} · 仅 PAID）</h3>
        <p style="margin:0 0 8px;color:#667085;font-size:12px;">
          eBay 已付款订单 × GIGA 采购成本快照 × 预估平台费（标「估」）。成本基数 = 货+运+保险+支付宝。
          全额/部分退款不计入 GMV/净利。
        </p>
        <table style="border-collapse:collapse;width:100%;font-size:13px;">
          <tr style="background:#eef6ff;">
            <td style="padding:6px 10px;border:1px solid #ddd;">PAID 订单</td>
            <td style="padding:6px 10px;border:1px solid #ddd;font-weight:bold;">{summary["order_count"]}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">GMV</td>
            <td style="padding:6px 10px;border:1px solid #ddd;font-weight:bold;">${summary["gmv"]:.2f}</td>
          </tr>
          <tr>
            <td style="padding:6px 10px;border:1px solid #ddd;">COGS (GIGA)</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">${summary["cogs"]:.2f}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">费用(估)</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">${summary["fees"]:.2f}</td>
          </tr>
          <tr style="background:#f9fafb;">
            <td style="padding:6px 10px;border:1px solid #ddd;">净利(估)</td>
            <td style="padding:6px 10px;border:1px solid #ddd;color:{net_color};font-weight:bold;">${summary["net"]:.2f}</td>
            <td style="padding:6px 10px;border:1px solid #ddd;">利润率(估)</td>
            <td style="padding:6px 10px;border:1px solid #ddd;color:{net_color};font-weight:bold;">{margin_pct:.1f}%</td>
          </tr>
          <tr>
            <td style="padding:6px 10px;border:1px solid #ddd;">履约</td>
            <td style="padding:6px 10px;border:1px solid #ddd;" colspan="3">
              未推 {not_pushed} · 未付 {pushed_unpaid} · 处理中 {processing} · 已发货 {shipped} · 异常 {errors}
            </td>
          </tr>
        </table>
        {alert_html}
        {loss_table}
        {not_pushed_table}
        <p style="color:#667085;font-size:11px;margin-top:8px;">看板: Streamlit「💰 财务」 · 同步: <code>scripts/finance_sync_orders.py</code></p>
        '''
    except Exception as e:
        logger.warning("render_finance_email_html failed: %s", e)
        return f'<h3>💰 财务摘要</h3><p style="color:red;">⚠️ 渲染失败: {_esc(str(e))}</p>'
    finally:
        if own_conn and conn is not None:
            conn.close()


def _esc(value: Any) -> str:
    s = str(value if value is not None else "")
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
