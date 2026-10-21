"""Reconcile local listing status to eBay ActiveList.

Dry-run by default. Pass --apply to write status / listing_id only.

This script never ends, relists, or revises anything on eBay.

Usage:
    python scripts/reconcile_live_listings.py
    python scripts/reconcile_live_listings.py --apply
    python scripts/reconcile_live_listings.py --out reports/live_reconcile
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.live_listing_reconcile import (
    build_reconcile_report,
    write_reconcile_reports,
)

DEFAULT_DB = PROJECT_ROOT / "ebay_collection.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile DB status/listing_id to eBay live listings (no eBay writes)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write status/listing_id. Default is dry-run.",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="Path to ebay_collection.db",
    )
    parser.add_argument(
        "--out",
        help="Report path prefix (writes .json and .csv). Default: reports/live_reconcile_<ts>",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from src.services.listing_status_sync import fetch_all_active_listings

    listings, api_total, fetch_complete = fetch_all_active_listings()
    if not fetch_complete:
        logging.warning(
            "ActiveList fetch incomplete (fetched=%d api_total=%d) — "
            "dry-run will still report mapped drift; --apply will restore "
            "live rows but will not mark ENDED from a partial snapshot",
            len(listings), api_total,
        )

    report = build_reconcile_report(
        args.db,
        listings,
        fetch_complete=fetch_complete,
        apply=bool(args.apply),
    )
    dest = Path(args.out) if args.out else (
        PROJECT_ROOT / "reports" / f"live_reconcile_{datetime.now():%Y%m%d_%H%M%S}"
    )
    json_path, csv_path = write_reconcile_reports(report, dest)
    logging.info(
        "Reconcile %s: live=%d changes=%d orphans=%d applied=%d json=%s csv=%s",
        "APPLY" if args.apply else "DRY-RUN",
        report.live_count,
        len(report.changes),
        len(report.orphans),
        sum(1 for row in report.changes if row.applied),
        json_path,
        csv_path,
    )
    print(json_path)
    print(csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
