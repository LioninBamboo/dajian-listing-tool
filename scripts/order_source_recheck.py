#!/usr/bin/env python3
"""Order-triggered source recheck — the last window to stop a refund.

When an order comes in, immediately re-fetch the GIGA/Dajian source for the
sold SKU, refresh the local snapshot, and diff the live eBay listing's claims
(dimensions, material, features) against the supplier's *current* truth.
Critical mismatches are emailed right away so the seller can contact the buyer
or adjust the listing before shipment.

Usage:
  python scripts/order_source_recheck.py                     # last 26h of orders
  python scripts/order_source_recheck.py --hours-back 4      # scheduled (2h cadence)
  python scripts/order_source_recheck.py --dry-run           # no DB writes, no email
  python scripts/order_source_recheck.py --sku W3636P456662  # force-check one SKU
"""

import argparse
import io
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name)
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
    elif getattr(_stream, "buffer", None) is not None:
        setattr(
            sys,
            _stream_name,
            io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace", line_buffering=True),
        )

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "ebay_collection.db"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

import os  # noqa: E402

from src.services.source_refresh import refresh_skus  # noqa: E402
from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations  # noqa: E402

_EBAY_NS = "{urn:ebay:apis:eBLBaseComponents}"

# Live measurement aspects vs fresh source attributes, with tolerance.
_MEASUREMENT_CHECKS = (
    ("Item Length", "Assembled Length (in.)", 1.0),
    ("Item Width", "Assembled Width (in.)", 1.0),
    ("Item Height", "Assembled Height (in.)", 1.0),
    ("Item Weight", "Product Weight (lbs.)", 2.0),
)


def parse_orders_xml(xml_text: str) -> tuple[list[dict], bool]:
    """Parse a GetOrders response into [{order_id, sku, item_id, created}] rows.

    Returns (rows, has_more_pages). Raises RuntimeError on an API failure Ack.
    """
    root = ET.fromstring(xml_text)
    ack = (root.findtext(f"{_EBAY_NS}Ack") or "").strip()
    if ack not in ("Success", "Warning"):
        short = root.findtext(f".//{_EBAY_NS}Errors/{_EBAY_NS}ShortMessage") or "unknown error"
        raise RuntimeError(f"GetOrders Ack={ack}: {short}")

    rows: list[dict] = []
    for order in root.iter(f"{_EBAY_NS}Order"):
        order_id = (order.findtext(f"{_EBAY_NS}OrderID") or "").strip()
        created = (order.findtext(f"{_EBAY_NS}CreatedTime") or "").strip()
        for txn in order.iter(f"{_EBAY_NS}Transaction"):
            sku = (
                txn.findtext(f"{_EBAY_NS}Variation/{_EBAY_NS}SKU")
                or txn.findtext(f"{_EBAY_NS}Item/{_EBAY_NS}SKU")
                or ""
            ).strip()
            item_id = (txn.findtext(f"{_EBAY_NS}Item/{_EBAY_NS}ItemID") or "").strip()
            if sku:
                rows.append({"order_id": order_id, "sku": sku, "item_id": item_id, "created": created})

    has_more = (root.findtext(f"{_EBAY_NS}HasMoreOrders") or "").strip().lower() == "true"
    return rows, has_more


def fetch_recent_orders(trading_client, hours_back: float) -> list[dict]:
    now = datetime.now(timezone.utc)
    time_from = (now - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    time_to = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    rows: list[dict] = []
    page = 1
    while True:
        payload = f"""
            <CreateTimeFrom>{time_from}</CreateTimeFrom>
            <CreateTimeTo>{time_to}</CreateTimeTo>
            <OrderRole>Seller</OrderRole>
            <OrderStatus>All</OrderStatus>
            <Pagination>
                <EntriesPerPage>100</EntriesPerPage>
                <PageNumber>{page}</PageNumber>
            </Pagination>
        """
        page_rows, has_more = parse_orders_xml(trading_client.call("GetOrders", payload))
        rows.extend(page_rows)
        if not has_more or page >= 20:
            break
        page += 1
    return rows


def ensure_recheck_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_recheck_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            sku TEXT NOT NULL,
            checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
            issues_json TEXT,
            alerted INTEGER DEFAULT 0,
            UNIQUE(order_id, sku)
        )
        """
    )
    conn.commit()


def already_checked(conn, order_id: str, sku: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM order_recheck_log WHERE order_id = ? AND sku = ?", (order_id, sku)
    ).fetchone()
    return row is not None


def _make_trading_client():
    """Trading API client (wraps the XML EbayClient, not RealEbayClient)."""
    from src.clients.ebay_client import EbayClient
    from src.clients.ebay_trading_client import EbayTradingClient

    ebay = EbayClient(
        os.getenv("EBAY_APP_ID"),
        os.getenv("EBAY_CERT_ID"),
        os.getenv("EBAY_DEV_ID"),
        env="production",
    )
    return EbayTradingClient(ebay)


def _aspect_first(aspects: dict, key: str) -> str:
    value = aspects.get(key)
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def _first_float(text: str):
    match = re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
    return float(match.group(1)) if match else None


def _fetch_live_content(ebay_client, sku: str, listing_id: str) -> dict | None:
    """Live title/description/aspects, with a Trading GetItem fallback for
    Inventory-blind (Trading-created) listings."""
    try:
        inventory = ebay_client.get_inventory_item(sku) or {}
    except Exception:
        inventory = {}
    product = inventory.get("product") or {}
    if product.get("title"):
        return {
            "title": product.get("title") or "",
            "description": product.get("description") or "",
            "aspects": product.get("aspects") or {},
        }

    if not listing_id:
        return None
    try:
        trading = _make_trading_client()
        xml_text = trading.get_item(listing_id)
        root = ET.fromstring(xml_text)
        item = root.find(f"{_EBAY_NS}Item")
        if item is None:
            return None
        aspects: dict[str, list[str]] = {}
        for nvl in item.iter(f"{_EBAY_NS}NameValueList"):
            name = (nvl.findtext(f"{_EBAY_NS}Name") or "").strip()
            values = [v.text.strip() for v in nvl.findall(f"{_EBAY_NS}Value") if v.text]
            if name and values:
                aspects[name] = values
        return {
            "title": (item.findtext(f"{_EBAY_NS}Title") or "").strip(),
            "description": item.findtext(f"{_EBAY_NS}Description") or "",
            "aspects": aspects,
        }
    except Exception:
        return None


def check_sku_against_fresh_source(conn, ebay_client, sku: str) -> list[dict]:
    """Diff the live listing against the (already refreshed) source snapshot."""
    row = conn.execute(
        "SELECT title, description, attributes, specs, listing_id FROM collected_products WHERE sku = ?",
        (sku,),
    ).fetchone()
    if row is None:
        return [{"type": "sku_not_in_db", "severity": "HIGH", "detail": f"{sku} not in collected_products"}]
    source_title, source_description, attributes_json, specs_json, listing_id = row
    attributes = json.loads(attributes_json or "{}")
    specs = json.loads(specs_json or "{}")

    live = _fetch_live_content(ebay_client, sku, listing_id)
    if live is None:
        return [{"type": "live_fetch_failed", "severity": "HIGH", "detail": "cannot read live listing content"}]

    issues: list[dict] = []

    constraints = build_source_constraints(
        attrs=attributes,
        specs=specs,
        source_description=source_description or "",
        source_title=source_title or "",
    )
    for violation in detect_claim_violations(
        source_constraints=constraints,
        generated_title=live["title"],
        generated_description=live["description"],
        generated_aspects=live["aspects"],
    ):
        if violation.severity in ("CRITICAL", "HIGH"):
            issues.append(
                {
                    "type": f"claim_{violation.claim_type}",
                    "severity": violation.severity,
                    "detail": f"{violation.claim_text} in {violation.location} (source={violation.source_evidence})",
                }
            )

    for aspect_key, attr_key, tolerance in _MEASUREMENT_CHECKS:
        live_value = _first_float(_aspect_first(live["aspects"], aspect_key))
        source_value = _first_float(attributes.get(attr_key))
        if live_value is not None and source_value is not None and abs(live_value - source_value) > tolerance:
            issues.append(
                {
                    "type": "measurement_drift",
                    "severity": "CRITICAL",
                    "detail": f"{aspect_key}: live={live_value} vs source={source_value} (>{tolerance} tolerance)",
                }
            )

    return issues


def build_clean_html(targets: list[dict]) -> str:
    """Positive confirmation that new orders were checked and matched source."""
    rows = "".join(
        f"<tr><td>{t.get('order_id','')}</td><td>{t.get('sku','')}</td></tr>" for t in targets
    )
    return f"""
    <h2>出单源复核 — 未发现问题</h2>
    <p>本次复核了 <b>{len(targets)}</b> 个新订单，重抓源数据并与 live listing 声明比对，
       未发现 CRITICAL 级不符。</p>
    <table border="1" cellpadding="6" style="border-collapse:collapse;">
      <tr><th>订单号</th><th>SKU</th></tr>{rows}
    </table>
    <p style="color:#999;font-size:12px;">来自 scripts/order_source_recheck.py —
       这封"无问题"回执是刻意发送的：没有邮件应当只意味着没有新订单，
       而不是复核跑失败了。</p>
    """


def build_alert_html(alerts: list[dict]) -> str:
    blocks = []
    for alert in alerts:
        rows = "".join(
            f"<tr><td>{i['severity']}</td><td>{i['type']}</td><td>{i['detail']}</td></tr>"
            for i in alert["issues"]
        )
        blocks.append(
            f"<h3>订单 {alert['order_id']} — SKU {alert['sku']}</h3>"
            f"<table border='1' cellpadding='6' style='border-collapse:collapse;'>"
            f"<tr><th>级别</th><th>类型</th><th>详情</th></tr>{rows}</table>"
        )
    return (
        "<h2>出单源复核告警</h2>"
        "<p>以下订单的 listing 声明与供应商当前源数据不符，请在发货前处理（联系买家或修正 listing），避免售后退款。</p>"
        + "".join(blocks)
        + "<p style=\"color:#999;font-size:12px;\">来自 scripts/order_source_recheck.py</p>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours-back", type=float, default=26.0, help="order lookback window")
    parser.add_argument("--sku", action="append", help="skip GetOrders and check this SKU directly")
    parser.add_argument("--dry-run", action="store_true", help="no DB writes, no email")
    parser.add_argument("--email", action="store_true", help="email critical mismatches")
    args = parser.parse_args()

    from src.clients.real_ebay_client import create_real_ebay_client

    ebay = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    dajian = None
    client_id, client_secret = os.getenv("DAJIAN_API_KEY"), os.getenv("DAJIAN_API_SECRET")
    if client_id and client_secret:
        from src.clients.dajian_client import DaJianClient

        dajian = DaJianClient(client_id, client_secret)

    conn = sqlite3.connect(DB_PATH)
    ensure_recheck_table(conn)

    if args.sku:
        targets = [{"order_id": f"manual-{datetime.now():%Y%m%d%H%M%S}", "sku": sku} for sku in args.sku]
    else:
        trading = _make_trading_client()
        # A single 30s read timeout used to fail the whole run with exit 1 and
        # raise a ❌ that looked like a real defect (2026-07-22 08:22). The task
        # runs every 6h over an 8h window, so coverage self-heals — but the
        # alarm did not, and real failures drown in that noise. Retry the flake.
        orders = None
        for attempt in range(3):
            try:
                orders = fetch_recent_orders(trading, args.hours_back)
                break
            except Exception as exc:
                if attempt == 2:
                    print(f"[ERROR] GetOrders failed after 3 attempts: {exc}")
                    return 1
                delay = 5 * (attempt + 1)
                print(f"[WARN] GetOrders attempt {attempt + 1}/3 failed ({exc}); retrying in {delay}s")
                time.sleep(delay)
        print(f"Orders in the last {args.hours_back:g}h: {len(orders)} line item(s)")
        targets = [o for o in orders if not already_checked(conn, o["order_id"], o["sku"])]
        print(f"New (unchecked) line items: {len(targets)}")

    alerts: list[dict] = []
    for target in targets:
        sku = target["sku"]
        order_id = target["order_id"]

        if dajian is not None and not args.dry_run:
            refresh_skus(conn, dajian, [sku], apply=True, context=f"order_recheck:{order_id}")

        issues = check_sku_against_fresh_source(conn, ebay, sku)
        critical = [i for i in issues if i["severity"] == "CRITICAL"]
        status = "CRITICAL" if critical else ("issues" if issues else "clean")
        print(f"  {order_id} / {sku}: {status}" + (f" — {len(issues)} issue(s)" if issues else ""))
        for issue in issues:
            print(f"      [{issue['severity']}] {issue['type']}: {issue['detail']}")

        if critical:
            alerts.append({"order_id": order_id, "sku": sku, "issues": issues})

        if not args.dry_run:
            conn.execute(
                "INSERT OR IGNORE INTO order_recheck_log (order_id, sku, issues_json, alerted) VALUES (?, ?, ?, ?)",
                (order_id, sku, json.dumps(issues, ensure_ascii=False), 1 if critical else 0),
            )
            conn.commit()

    # Mail on every order actually checked, not only on conflicts. Silence used
    # to mean three different things — "checked, clean", "never checked", and
    # "the task is broken" — and on 2026-07-27..29 three orders shipped with no
    # mail at all: one run was skipped, another died on a DNS failure, and the
    # clean one sent nothing by design. A clean run must be visibly clean.
    if targets and args.email and not args.dry_run:
        from src.utils.email_sender import send_email

        if alerts:
            send_email(
                f"⚠ 出单源复核: {len(alerts)} 个订单的 listing 与源不符",
                build_alert_html(alerts),
            )
        else:
            send_email(
                f"✅ 出单源复核: {len(targets)} 个新订单已复核，未发现与源不符",
                build_clean_html(targets),
            )
    elif alerts:
        print(f"{len(alerts)} critical alert(s) (email suppressed: use --email without --dry-run)")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
