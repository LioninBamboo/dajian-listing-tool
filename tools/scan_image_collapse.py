"""Scan all PUBLISHED listings and report any whose live `inventory_item.product.imageUrls`
count is significantly lower than the local DB source images. Use
`tools/restore_listing_images.py SKU [SKU ...]` to fix flagged ones.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()
warnings.filterwarnings("ignore")

from src.services.ebay_auth import EbayOAuthService  # noqa: E402

DB_PATH = PROJECT_ROOT / "ebay_collection.db"
OUT_PATH = PROJECT_ROOT / "logs" / "image_collapse_scan.json"


def check_sku(sku: str, expected: int, headers: dict) -> tuple[str, int, int] | None:
    try:
        resp = requests.get(
            f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}",
            headers=headers, timeout=20, verify=False,
        )
        if resp.status_code != 200:
            return None
        live = (resp.json().get("product") or {}).get("imageUrls") or []
        if len(live) < max(2, expected - 1) and expected >= 3:
            return (sku, len(live), expected)
    except Exception:
        return None
    return None


def main() -> int:
    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    token = oauth.get_valid_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sku, images FROM collected_products WHERE status='PUBLISHED'"
    ).fetchall()
    conn.close()

    candidates: list[tuple[str, int]] = []
    for r in rows:
        if not r["images"]:
            continue
        try:
            imgs = json.loads(r["images"])
        except Exception:
            continue
        if imgs:
            candidates.append((r["sku"], min(len(imgs), 24)))

    print(f"scanning {len(candidates)} published listings", flush=True)
    bad: list[tuple[str, int, int]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(check_sku, sku, exp, headers): sku for sku, exp in candidates}
        for fut in as_completed(futures):
            done += 1
            if done % 25 == 0:
                print(f"  progress {done}/{len(candidates)} — bad so far {len(bad)}", flush=True)
            res = fut.result()
            if res:
                bad.append(res)
                print(f"  [BAD] {res[0]}: live={res[1]} expected={res[2]}", flush=True)

    bad.sort()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(bad, indent=2), encoding="utf-8")
    print(f"\n=== {len(bad)} degraded listings written to {OUT_PATH}", flush=True)
    if bad:
        print("SKUs:", " ".join(s for s, _, _ in bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
