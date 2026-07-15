#!/usr/bin/env python3
"""Scheduled source-content refresh for PUBLISHED listings.

Re-fetches the GIGA/Dajian product detail for every published SKU, detects
seller-side content drift (title / params / copy / video), repairs the local
source snapshot, and reports drifted SKUs so the daily live audit (11:30)
compares live listings against the supplier's current truth.

Usage:
  python scripts/source_content_refresh.py                 # refresh all PUBLISHED
  python scripts/source_content_refresh.py --dry-run       # detect drift, no writes
  python scripts/source_content_refresh.py --sku W3636P456662
  python scripts/source_content_refresh.py --email         # email drift summary
"""

import argparse
import io
import json
import os
import sqlite3
import sys
from datetime import datetime
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

from src.services.source_refresh import refresh_skus  # noqa: E402


def get_dajian_client():
    from src.clients.dajian_client import DaJianClient

    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    if not client_id or not client_secret:
        return None
    return DaJianClient(client_id, client_secret)


def load_target_skus(conn, args) -> list[str]:
    if args.sku:
        return list(args.sku)
    if args.sku_file:
        lines = Path(args.sku_file).read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip()]
    rows = conn.execute(
        "SELECT sku FROM collected_products WHERE status = 'PUBLISHED' ORDER BY updated_at DESC"
    ).fetchall()
    return [row[0] for row in rows]


def build_email_html(summary: dict) -> str:
    alerts = summary.get("alerts", {})
    rows = []
    for sku, changes in sorted(alerts.items()):
        detail = "<br>".join(
            f"{c['field']}: {str(c.get('old'))[:80]} → {str(c.get('new'))[:80]}" for c in changes
        )
        rows.append(f"<tr><td>{sku}</td><td>{detail}</td></tr>")
    table = (
        "<table border='1' cellpadding='6' style='border-collapse:collapse;'>"
        "<tr><th>SKU</th><th>卖家变更内容</th></tr>" + "".join(rows) + "</table>"
        if rows
        else "<p>无卖家侧内容变更。</p>"
    )
    return f"""
    <h2>源内容刷新报告</h2>
    <p>检查 {summary.get('checked', 0)} 个 PUBLISHED SKU：
       卖家内容变更 {len(alerts)}，基线补全/归位 {len(summary.get('drifted', {})) - len(alerts)}，
       快照已更新 {len(summary.get('applied', []))}，
       源不可购 {len(summary.get('unavailable', []))}，抓取失败 {len(summary.get('fetch_failed', []))}。</p>
    <h3>卖家侧变更（需跟进 live listing）</h3>
    {table}
    <p>源不可购: {', '.join(summary.get('unavailable', [])) or '无'}</p>
    <p style="color:#999;font-size:12px;">来自 scripts/source_content_refresh.py</p>
    """


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sku", action="append", help="only refresh this SKU (repeatable)")
    parser.add_argument("--sku-file", help="file with one SKU per line")
    parser.add_argument("--dry-run", action="store_true", help="detect drift but do not write")
    parser.add_argument("--email", action="store_true", help="email the drift summary")
    parser.add_argument("--limit", type=int, default=0, help="cap the number of SKUs")
    args = parser.parse_args()

    dajian = get_dajian_client()
    if dajian is None:
        print("[ERROR] DAJIAN_API_KEY/DAJIAN_API_SECRET missing — cannot refresh sources")
        return 1

    conn = sqlite3.connect(DB_PATH)
    try:
        skus = load_target_skus(conn, args)
        if args.limit:
            skus = skus[: args.limit]
        print(f"Refreshing source snapshots for {len(skus)} SKU(s) (dry_run={args.dry_run})")

        summary = refresh_skus(
            conn,
            dajian,
            skus,
            apply=not args.dry_run,
            context="scheduled_refresh" if not args.sku else "manual_refresh",
        )
    finally:
        conn.close()

    drifted = summary.get("drifted", {})
    alerts = summary.get("alerts", {})
    print(
        f"checked={summary['checked']} seller_changes={len(alerts)} "
        f"baseline_updates={len(drifted) - len(alerts)} applied={len(summary['applied'])} "
        f"unavailable={len(summary['unavailable'])} fetch_failed={len(summary['fetch_failed'])}"
    )
    for sku, changes in sorted(alerts.items()):
        fields = ", ".join(sorted({c["field"] for c in changes}))
        print(f"  SELLER-CHANGE {sku}: {fields}")
    for sku, drifts in sorted(drifted.items()):
        if sku in alerts:
            continue
        fields = ", ".join(sorted({d["field"] for d in drifts}))
        print(f"  baseline {sku}: {fields}")

    report_path = LOG_DIR / f"source_refresh_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {report_path}")

    # Hand seller-changed SKUs to the live audit so it re-checks them against fresh truth.
    if alerts and not args.dry_run:
        sku_file = LOG_DIR / "source_drift_skus.txt"
        sku_file.write_text("\n".join(sorted(alerts)) + "\n", encoding="utf-8")
        print(f"Seller-changed SKU list for audit: {sku_file}")

    if args.email:
        from src.utils.email_sender import send_email

        subject = f"源内容刷新: {len(alerts)} 个卖家变更 / {summary['checked']} 检查"
        send_email(subject, build_email_html(summary))

    return 0


if __name__ == "__main__":
    sys.exit(main())
