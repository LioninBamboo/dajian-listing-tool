"""Restore multi-image listings whose live `imageUrls` got reduced.

Safe repair flow:
  1. GET current inventory_item.
  2. Upload local DB `images` to eBay EPS (one-time host).
  3. PUT inventory_item with EPS imageUrls (preserve everything else).
  4. Republish the offer so live listing reflects the new images.

Why this exists: several audit/fix scripts historically overwrote
`product.imageUrls` with raw GigaB2B signed URLs (`x-cs=...`). eBay then
fetches those URLs, most fail (signature scoping), and the live listing
collapses down to whichever single fetch succeeded. Always upload to EPS.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import warnings
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from src.services.ebay_auth import EbayOAuthService  # noqa: E402
from src.clients.real_ebay_client import create_real_ebay_client  # noqa: E402

warnings.filterwarnings("ignore")

DB_PATH = PROJECT_ROOT / "ebay_collection.db"


def restore_one(sku: str, ebay_client, oauth: EbayOAuthService) -> bool:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT images FROM collected_products WHERE sku = ?",
        (sku,),
    ).fetchone()
    conn.close()

    if not row or not row["images"]:
        print(f"[{sku}] no DB images, skip")
        return False

    src_images = json.loads(row["images"])
    print(f"[{sku}] DB has {len(src_images)} source images")

    token = oauth.get_valid_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
        "Accept": "application/json",
    }

    inv_url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"
    inv_resp = requests.get(inv_url, headers=headers, timeout=30, verify=False)
    if inv_resp.status_code != 200:
        print(f"[{sku}] inventory GET failed: {inv_resp.status_code}")
        return False

    inv = inv_resp.json()
    current_images = (inv.get("product") or {}).get("imageUrls") or []
    print(f"[{sku}] live inventory currently has {len(current_images)} imageUrls")

    if len(current_images) >= max(2, min(len(src_images), 24) - 1):
        print(f"[{sku}] already has enough images, no-op")
        return True

    # Upload DB images to EPS so eBay hosts them permanently.
    eps_urls = ebay_client.upload_images_to_eps(src_images, max_images=24)
    if not eps_urls:
        print(f"[{sku}] EPS upload returned 0 images, abort")
        return False
    print(f"[{sku}] uploaded {len(eps_urls)} images to EPS")

    # Strip read-only fields then patch imageUrls only.
    inv.pop("sku", None)
    inv.pop("locale", None)
    pws = inv.get("packageWeightAndSize") or {}
    w = (pws.get("weight") or {}).get("value", 0)
    try:
        if float(w) <= 0:
            inv.pop("packageWeightAndSize", None)
    except (TypeError, ValueError):
        inv.pop("packageWeightAndSize", None)
    avail = inv.get("availability") or {}
    ship = avail.get("shipToLocationAvailability") or {}
    ship.pop("allocationByFormat", None)

    inv.setdefault("product", {})
    inv["product"]["imageUrls"] = eps_urls

    put_resp = requests.put(inv_url, headers=headers, json=inv, timeout=60, verify=False)
    if put_resp.status_code not in (200, 204):
        print(f"[{sku}] inventory PUT failed: {put_resp.status_code} {put_resp.text[:200]}")
        return False
    print(f"[{sku}] inventory PUT ok")

    # Republish offer so live listing picks up the new images.
    offers_resp = requests.get(
        "https://api.ebay.com/sell/inventory/v1/offer",
        headers=headers,
        params={"sku": sku},
        timeout=30,
        verify=False,
    )
    if offers_resp.status_code != 200:
        print(f"[{sku}] offers GET failed: {offers_resp.status_code}")
        return False
    offers = offers_resp.json().get("offers") or []
    if not offers:
        print(f"[{sku}] no offer found")
        return False
    offer_id = offers[0].get("offerId")
    pub_resp = requests.post(
        f"https://api.ebay.com/sell/inventory/v1/offer/{offer_id}/publish",
        headers=headers,
        timeout=60,
        verify=False,
    )
    if pub_resp.status_code != 200:
        print(f"[{sku}] offer publish failed: {pub_resp.status_code} {pub_resp.text[:200]}")
        return False

    # Verify
    verify_resp = requests.get(inv_url, headers=headers, timeout=30, verify=False)
    if verify_resp.status_code == 200:
        live_count = len((verify_resp.json().get("product") or {}).get("imageUrls") or [])
        print(f"[{sku}] OK — live inventory now has {live_count} imageUrls")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("skus", nargs="*", help="SKU(s) to restore")
    parser.add_argument("--from-file", dest="from_file", help="File with one SKU per line")
    parser.add_argument("--workers", type=int, default=1, help="Parallel SKU workers (default 1)")
    args = parser.parse_args()

    skus: list[str] = list(args.skus or [])
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            skus.extend(s.strip() for s in f if s.strip())
    if not skus:
        print("no SKUs provided")
        return 2

    env = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = EbayOAuthService(env)
    if not oauth.is_authorized():
        print("eBay not authorized")
        return 2

    ebay_client = create_real_ebay_client(env)

    failed = 0
    total = len(skus)
    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from threading import Lock
        counter = {"i": 0}
        lock = Lock()

        def _worker(sku: str) -> tuple[str, bool, str | None]:
            try:
                ok = restore_one(sku, ebay_client, oauth)
                return (sku, ok, None)
            except Exception as e:
                return (sku, False, str(e))

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(_worker, s): s for s in skus}
            for fut in as_completed(futures):
                sku, ok, err = fut.result()
                with lock:
                    counter["i"] += 1
                    print(f"--- [{counter['i']}/{total}] {sku} {'OK' if ok else 'FAIL'} {err or ''}", flush=True)
                if not ok:
                    failed += 1
    else:
        for i, sku in enumerate(skus, 1):
            print(f"--- [{i}/{total}] {sku} ---", flush=True)
            try:
                if not restore_one(sku, ebay_client, oauth):
                    failed += 1
            except Exception as e:
                print(f"[{sku}] exception: {e}", flush=True)
                failed += 1
    print(f"\n=== restore done: {total - failed}/{total} ok, {failed} failed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
