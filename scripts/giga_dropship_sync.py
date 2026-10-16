#!/usr/bin/env python
"""Phase 2: poll GIGA order status/tracking and write back eBay shipping fulfillments.

Default is dry-run (shows what would be sent to eBay). Use --apply to actually
mark eBay orders shipped with tracking.

Examples:
  python scripts/giga_dropship_sync.py --dry-run
  python scripts/giga_dropship_sync.py --order 04-15027-11320 --dry-run
  python scripts/giga_dropship_sync.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(description="GIGA status/track → eBay fulfill sync")
    parser.add_argument("--order", default="", help="Only this eBay order id")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", help="Write eBay shipping fulfillment")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "ebay_collection.db"))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    dry_run = not args.apply
    os.environ.setdefault("EBAY_ENVIRONMENT", "PRODUCTION")

    from src.services.giga_dropship_sync import sync_pending_fulfillments

    try:
        results = sync_pending_fulfillments(
            db_path=args.db,
            dry_run=dry_run,
            ebay_order_id=args.order or None,
        )
    except Exception as e:
        print(f"[ERROR] {e}")
        return 1

    if not results:
        print("No pending fulfillment rows (status pushed/still_processing/...).")
        print("Push orders first: python scripts/giga_dropship_push.py --apply")
        return 0

    for r in results:
        st = r.get("status")
        oid = r.get("ebay_order_id")
        tracks = r.get("tracking") or []
        t0 = tracks[0] if tracks else {}
        print(
            f"  [{st}] {oid} giga={r.get('giga_order_no')} "
            f"giga_status={r.get('giga_status')} "
            f"track={t0.get('trackingNum','-')} carrier={t0.get('carrierName','-')}"
            + (f" err={r.get('error')}" if r.get("error") else "")
        )

    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "reports" / f"giga_dropship_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "timestamp": datetime.now().isoformat(),
        "dry_run": dry_run,
        "count": len(results),
        "results": results,
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
