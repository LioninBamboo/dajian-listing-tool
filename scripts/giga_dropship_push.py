#!/usr/bin/env python
"""Phase 1: eBay unpaid-ship orders → GIGA dropship payload (dry-run by default).

GIGA one-piece dropship chooses the warehouse — we never send warehouseCode.

Examples:
  # List unfulfilled eBay orders + build payloads (no GIGA write)
  python scripts/giga_dropship_push.py --days 14 --dry-run

  # Single order
  python scripts/giga_dropship_push.py --order 04-15027-11320 --dry-run

  # Live push (requires confirmation flag)
  python scripts/giga_dropship_push.py --order 04-15027-11320 --apply

Docs: docs/GIGA_FULFILLMENT_API.md, docs/GIGA_FULFILLMENT_FEATURE_PLAN.md
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")


def main():
    parser = argparse.ArgumentParser(description="eBay → GIGA dropship push (dry-run default)")
    parser.add_argument("--days", type=int, default=14, help="Look back N days of eBay orders")
    parser.add_argument("--order", default="", help="Only this eBay orderId / legacyOrderId")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Build/validate only (default)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually call GIGA dropShip-sync (disables dry-run)",
    )
    parser.add_argument(
        "--include-pushed",
        action="store_true",
        help="Do not skip orders already marked pushed/shipped in local DB",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Write plan JSON to this path (default reports/giga_dropship_plan_*.json)",
    )
    parser.add_argument("--db", default=str(PROJECT_ROOT / "ebay_collection.db"))
    args = parser.parse_args()

    dry_run = not args.apply

    from src.services.giga_dropship import (
        fetch_ebay_orders,
        find_inventory_shortages,
        is_ambiguous_dropship_push_error,
        plan_dropship_batch,
        record_fulfillment_attempt,
        reserve_fulfillment_push,
        ensure_fulfillment_table,
    )
    from src.clients.dajian_client import DaJianClient

    print(f"Fetching eBay orders (last {args.days} days)...")
    try:
        orders = fetch_ebay_orders(days=args.days)
    except Exception as e:
        print(f"[ERROR] eBay Fulfillment fetch failed: {e}")
        return 1
    print(f"  total orders pulled: {len(orders)}")

    dajian = None
    if not dry_run:
        key = os.getenv("DAJIAN_API_KEY") or os.getenv("DAJIAN_CLIENT_ID")
        secret = os.getenv("DAJIAN_API_SECRET") or os.getenv("DAJIAN_CLIENT_SECRET")
        if not key or not secret:
            print("[ERROR] --apply requires DAJIAN_API_KEY / DAJIAN_API_SECRET")
            return 1
        dajian = DaJianClient(key, secret)

    conn = sqlite3.connect(args.db)
    ensure_fulfillment_table(conn)

    plans = plan_dropship_batch(
        orders,
        only_order_id=args.order or None,
        skip_already_pushed=not args.include_pushed,
        conn=conn,
    )
    print(f"  dropship candidates: {len(plans)}")

    results = []

    for plan in plans:
        ebay_id = plan["ebay_order_id"]
        payload = plan["payload"]
        giga_no = payload.get("orderNo")
        row = {
            "ebay_order_id": ebay_id,
            "giga_order_no": giga_no,
            "summary": plan["summary"],
            "validation_errors": plan["validation_errors"],
            "prior_status": plan["prior_status"],
            "ok": plan["ok"],
            "dry_run": dry_run,
        }

        if not plan["ok"]:
            print(f"  [INVALID] {ebay_id}: {plan['validation_errors']}")
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="validation_failed",
                payload=payload,
                error="; ".join(plan["validation_errors"]),
            )
            row["status"] = "validation_failed"
            results.append(row)
            continue

        if dry_run:
            # Validate through client dry_run path too
            try:
                DaJianClient("x", "y").import_dropship_order(payload, dry_run=True)
            except ValueError as e:
                print(f"  [INVALID] {ebay_id}: {e}")
                row["status"] = "validation_failed"
                row["error"] = str(e)
                results.append(row)
                continue
            print(
                f"  [DRY-RUN OK] {ebay_id} → {giga_no} "
                f"skus={plan['summary'].get('skus')} total={plan['summary'].get('total')}"
            )
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="dry_run_ready",
                payload=payload,
            )
            row["status"] = "dry_run_ready"
            row["payload"] = payload
            results.append(row)
            continue

        # Live apply
        skus = sorted(
            {
                str(line.get("sku") or "").strip()
                for line in payload.get("orderLines") or []
                if str(line.get("sku") or "").strip()
            }
        )

        try:
            inventory_rows = dajian.get_inventory(skus)
            inventory_by_sku = {
                str(item.get("sku") or "").strip(): item
                for item in inventory_rows
                if isinstance(item, dict) and str(item.get("sku") or "").strip()
            }
        except Exception as e:
            print(f"  [STOCK CHECK FAILED] {ebay_id}: {e}")
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="stock_check_failed",
                payload=payload,
                error=str(e),
            )
            row["status"] = "stock_check_failed"
            row["error"] = str(e)
            results.append(row)
            continue

        shortages = find_inventory_shortages(payload, inventory_by_sku)
        if shortages:
            print(f"  [STOCK BLOCKED] {ebay_id}: {shortages}")
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="stock_blocked",
                payload=payload,
                error="; ".join(shortages),
            )
            row["status"] = "stock_blocked"
            row["error"] = "; ".join(shortages)
            results.append(row)
            continue

        try:
            # Reserve local idempotency before the network write. If the API
            # times out after accepting the order, later runs must not re-push.
            reserved = reserve_fulfillment_push(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                payload=payload,
            )
            if not reserved:
                row["status"] = "skipped_reserved"
                row["error"] = "an existing fulfillment row already owns this order"
                results.append(row)
                continue
            resp = dajian.import_dropship_order(payload, dry_run=False)
            print(f"  [PUSHED] {ebay_id} → {giga_no}")
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status="pushed",
                payload=payload,
            )
            row["status"] = "pushed"
            row["giga_response"] = resp
        except Exception as e:
            status = (
                "push_unknown"
                if is_ambiguous_dropship_push_error(e)
                else "push_failed"
            )
            print(f"  [{status.upper()}] {ebay_id}: {e}")
            record_fulfillment_attempt(
                conn,
                ebay_order_id=ebay_id,
                giga_order_no=giga_no,
                status=status,
                payload=payload,
                error=str(e),
            )
            row["status"] = status
            row["error"] = str(e)
        results.append(row)

    conn.close()

    out_path = Path(args.out) if args.out else (
        PROJECT_ROOT
        / "reports"
        / f"giga_dropship_plan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": datetime.now().isoformat(),
        "dry_run": dry_run,
        "days": args.days,
        "orders_pulled": len(orders),
        "candidates": len(plans),
        "results": results,
        "note": "Dropship does not set warehouseCode; GIGA selects ship-from warehouse.",
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    print(
        f"Summary: ok={sum(1 for r in results if r.get('status') in ('dry_run_ready','pushed'))} "
        f"invalid={sum(1 for r in results if r.get('status')=='validation_failed')} "
        f"unknown={sum(1 for r in results if r.get('status')=='push_unknown')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
