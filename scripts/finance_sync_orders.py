#!/usr/bin/env python
"""Finance F0: sync eBay paid orders into finance_order tables with GIGA cost PnL.

Usage:
  python scripts/finance_sync_orders.py --days 30
  python scripts/finance_sync_orders.py --days 7 --ad-rate 0.05
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
    parser = argparse.ArgumentParser(description="Sync eBay orders → finance PnL tables")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--ad-rate", type=float, default=0.05, help="Estimated ad rate (default 5%)")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "ebay_collection.db"))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    os.environ.setdefault("EBAY_ENVIRONMENT", "PRODUCTION")

    from src.services.finance_orders import sync_orders_from_ebay, query_finance_summary
    import sqlite3

    print(f"Syncing eBay orders (last {args.days} days), ad_rate={args.ad_rate:.0%} ...")
    try:
        report = sync_orders_from_ebay(
            days=args.days,
            db_path=args.db,
            ad_rate=args.ad_rate,
        )
    except Exception as e:
        print(f"[ERROR] {e}")
        return 1

    conn = sqlite3.connect(args.db)
    summary = query_finance_summary(conn)
    conn.close()

    print(
        f"Pulled={report['orders_pulled']} upserted={report['upserted']} "
        f"errors={report['errors']}"
    )
    print(
        f"Summary (PAID): orders={summary['order_count']} "
        f"GMV=${summary['gmv']:.2f} COGS=${summary['cogs']:.2f} "
        f"Fees(est)=${summary['fees']:.2f} Net(est)=${summary['net']:.2f} "
        f"Margin={summary['margin']*100:.1f}% loss_orders={summary['loss_orders']}"
    )
    print(
        f"Fulfill: not_pushed={summary.get('not_pushed', 0)} "
        f"pushed_unpaid={summary.get('pushed_unpaid', 0)} "
        f"shipped={summary.get('shipped', 0)} "
        f"stages={summary.get('fulfill_stages')}"
    )
    print(f"GIGA links updated: {report.get('giga_links_updated', 0)}")

    out = Path(args.out) if args.out else (
        PROJECT_ROOT / "reports" / f"finance_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"timestamp": datetime.now().isoformat(), "sync": report, "summary": summary},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
