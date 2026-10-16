"""Audit published listings with source videos and repair missing eBay video links."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.clients.real_ebay_client import create_real_ebay_client
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_video_uploader import EbayVideoUploader
from src.utils.title_sanitizer import sanitize_listing_title


DB_PATH = ROOT / "ebay_collection.db"


def _parse_json(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return json.loads(value)
    except Exception:
        return {}


def _parse_list(value):
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, str) and item.strip()]
    return []


def _update_local_video_state(sku: str, *, video_id: str | None, status: str | None) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    row = cur.execute("SELECT optimization FROM collected_products WHERE sku = ?", (sku,)).fetchone()
    if row:
        opt = _parse_json(row[0])
        if video_id:
            opt["video_id"] = video_id
        if status:
            opt["video_status"] = status
        cur.execute(
            "UPDATE collected_products SET optimization = ? WHERE sku = ?",
            (json.dumps(opt, ensure_ascii=False), sku),
        )
        conn.commit()
    conn.close()


def get_products_with_source_videos(*, sku: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT sku, title, videos, optimization, listing_id
        FROM collected_products
        WHERE status = 'PUBLISHED'
          AND listing_id IS NOT NULL
          AND videos IS NOT NULL
          AND videos != '[]'
    """
    params = []
    if sku:
        query += " AND sku = ?"
        params.append(sku)
    query += " ORDER BY sku"
    rows = conn.execute(query, tuple(params)).fetchall()
    conn.close()

    products = []
    for row in rows:
        opt = _parse_json(row["optimization"])
        videos = _parse_list(row["videos"])
        if not videos:
            continue
        if opt.get("video_status") == "UNSUPPORTED_SOURCE":
            continue
        clean_title, _ = sanitize_listing_title(opt.get("title") or row["title"] or "")
        products.append(
            {
                "sku": row["sku"],
                "listing_id": row["listing_id"],
                "title": clean_title[:50] or "Product Video",
                "video_url": videos[0],
                "video_id": opt.get("video_id"),
                "video_status": opt.get("video_status"),
            }
        )
    return products


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Audit and repair missing eBay videos")
    parser.add_argument("--sku", help="Only repair the specified SKU")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    print("=" * 60)
    print("Audit and repair missing eBay videos")
    print("=" * 60)

    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = EbayOAuthService(environment)
    if not oauth.is_authorized():
        print("[Error] Not authorized. Please run OAuth flow first.")
        return

    uploader = EbayVideoUploader(oauth)
    ebay_client = create_real_ebay_client(environment)

    products = get_products_with_source_videos(sku=args.sku)
    if not products:
        if args.sku:
            print(f"\nNo published listing with source videos found for {args.sku}.")
        else:
            print("\nNo published listings with source videos found.")
        return

    print(f"\nFound {len(products)} published listings with source videos.\n")

    repaired = 0
    already_ok = 0
    failed = 0

    for item in products:
        sku = item["sku"]
        local_video_id = item["video_id"]
        local_status = item["video_status"]

        print(f"[{sku}] {item['title']}")

        try:
            inventory = ebay_client.get_inventory_item(sku) or {}
            live_video_ids = ((inventory.get("product") or {}).get("videoIds") or [])

            if live_video_ids:
                print(f"  OK live videoIds: {live_video_ids}")
                if not local_video_id:
                    _update_local_video_state(sku, video_id=live_video_ids[0], status="LIVE")
                    print(f"  Synced local video_id from eBay: {live_video_ids[0]}")
                already_ok += 1
                continue

            if local_video_id:
                status = uploader.get_video_status(local_video_id)
                status_value = str((status or {}).get("status", "")).upper()
                if status_value == "LIVE":
                    print(f"  Linking existing LIVE video {local_video_id} to inventory")
                    if uploader._add_video_to_ebay_inventory(sku, local_video_id):
                        _update_local_video_state(sku, video_id=local_video_id, status="LIVE")
                        repaired += 1
                    else:
                        print("  FAILED could not attach existing LIVE video to inventory")
                        failed += 1
                    continue
                print(f"  Existing local video {local_video_id} is {status_value or local_status or 'UNKNOWN'}; re-uploading")

            video_id = uploader.upload_video_sync(item["video_url"], sku, item["title"])
            if video_id:
                inventory = ebay_client.get_inventory_item(sku) or {}
                live_video_ids = ((inventory.get("product") or {}).get("videoIds") or [])
                if video_id in live_video_ids:
                    print(f"  Uploaded and linked video: {video_id}")
                    _update_local_video_state(sku, video_id=video_id, status="LIVE")
                    repaired += 1
                else:
                    print(f"  FAILED uploaded video {video_id} but it is not attached to live inventory")
                    _update_local_video_state(sku, video_id=video_id, status="UPLOADED")
                    failed += 1
            else:
                print("  FAILED upload_video_sync returned no video_id")
                failed += 1
        except Exception as exc:
            print(f"  FAILED: {exc}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"Done. repaired={repaired}, already_ok={already_ok}, failed={failed}")
    print("=" * 60)


if __name__ == "__main__":
    main()
