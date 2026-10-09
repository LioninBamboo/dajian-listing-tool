#!/usr/bin/env python3
"""Backfill source video URLs from DaJian detail into collected_products."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from src.clients.dajian_client import DaJianClient, extract_product_video_urls

DB_PATH = ROOT / "ebay_collection.db"


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Backfill source video URLs for collected SKUs")
    parser.add_argument("--sku", required=True, help="Target SKU to refresh from DaJian detail")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing non-empty videos in collected_products",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    if not client_id or not client_secret:
        print("DAJIAN_API_KEY / DAJIAN_API_SECRET missing")
        return 2

    dajian = DaJianClient(client_id, client_secret)
    detail = dajian.get_product_detail_by_sku(args.sku)
    video_urls = extract_product_video_urls(detail)
    if not video_urls:
        print(f"{args.sku}: no source videos found in DaJian detail")
        return 1

    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT videos, logs, optimization FROM collected_products WHERE sku = ?",
            (args.sku,),
        ).fetchone()
        if not row:
            print(f"{args.sku}: SKU not found in collected_products")
            return 1

        existing_videos = json.loads(row[0]) if row[0] else []
        if existing_videos and not args.force:
            print(f"{args.sku}: existing videos present, skipping (use --force to overwrite)")
            return 0

        logs = json.loads(row[1]) if row[1] else []
        optimization = json.loads(row[2]) if row[2] else {}
        if optimization.get("video_status") == "UNSUPPORTED_SOURCE":
            optimization.pop("video_status", None)

        logs.append(
            f"Source videos backfilled from DaJian API at {_utcnow_naive().isoformat()}: {len(video_urls)}"
        )

        conn.execute(
            "UPDATE collected_products SET videos = ?, optimization = ?, logs = ?, updated_at = ? WHERE sku = ?",
            (
                json.dumps(video_urls, ensure_ascii=False),
                json.dumps(optimization, ensure_ascii=False),
                json.dumps(logs, ensure_ascii=False),
                _utcnow_naive().isoformat(sep=" "),
                args.sku,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"{args.sku}: backfilled {len(video_urls)} source video(s)")
    for idx, url in enumerate(video_urls, start=1):
        print(f"  {idx}. {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
