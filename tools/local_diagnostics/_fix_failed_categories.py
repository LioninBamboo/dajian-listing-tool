"""
Fix the 5 listings that failed category update due to missing required aspects.
Must update inventory item (with required aspects) THEN update offer category.
"""
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / 'ebay_collection.db'
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
from src.clients.real_ebay_client import RealEbayClient

# Only W5633P453797 still needs category change (others already fixed)
FIXES = [
    {
        "sku": "W5633P453797",
        "new_cat": "79691",
        "reason": "Hard Cooler → Ice Chests & Coolers (79691)",
        "aspect_patch": {"Type": ["Hard Cooler"], "Brand": ["Unbranded"]},
    },
]

# SKUs that were already fixed on eBay but local DB may need update
DB_ONLY_FIXES = [
    ("W3916P426287", "183316"),
    ("N759P307032D", "25458"),
    ("W1143141068", "25458"),
    ("W808P362279", "66756"),
]


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    dry_run = not args.apply

    print(f"{'DRY RUN' if dry_run else '*** LIVE ***'}: Fixing {len(FIXES)} listings\n")

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    policy_mgr = EbayPolicyManager(oauth)
    client = RealEbayClient(oauth, policy_mgr)

    for fix in FIXES:
        sku = fix["sku"]
        new_cat = fix["new_cat"]
        reason = fix["reason"]
        aspect_patch = fix["aspect_patch"]

        row = conn.execute(
            "SELECT optimization, listing_id FROM collected_products WHERE sku=?", (sku,)
        ).fetchone()
        if not row:
            print(f"  [{sku}] Not found")
            continue

        opt = json.loads(row["optimization"]) if row["optimization"] else {}
        listing_id = row["listing_id"]
        aspects = opt.get("aspects", {})
        title = opt.get("title", "")[:80]
        description = opt.get("description", "")

        # Apply aspect patches
        for k, v in aspect_patch.items():
            if k not in aspects or not aspects[k]:
                aspects[k] = v
                print(f"  [{sku}] Adding aspect {k}={v}")
            else:
                print(f"  [{sku}] Aspect {k} already present: {aspects[k]}")

        print(f"  [{sku}] #{listing_id}: → {new_cat} ({reason})")

        if dry_run:
            print(f"    DRY: Would update inventory + offer")
            continue

        try:
            # Step 1: GET existing inventory item (full payload)
            inv = client.get_inventory_item(sku)
            if not inv:
                print(f"    [{sku}] No inventory item found on eBay")
                continue

            # Step 2: Merge aspects into existing inventory payload
            existing_aspects = inv.get("product", {}).get("aspects", {})
            for k, v in aspect_patch.items():
                existing_aspects[k] = v
            inv["product"]["aspects"] = existing_aspects

            # Step 3: Fix invalid weight (eBay rejects 0.0)
            pkg = inv.get("packageWeightAndSize", {})
            weight_val = pkg.get("weight", {}).get("value", 0)
            if weight_val == 0 or weight_val == 0.0:
                inv.pop("packageWeightAndSize", None)
                print(f"    [{sku}] Removed invalid weight=0.0")

            # Step 3b: PUT updated inventory item directly (preserves everything else)
            import requests
            token = oauth.get_valid_token()
            put_url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{sku}"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Language": "en-US",
            }
            resp = requests.put(put_url, headers=headers, json=inv, timeout=60)
            if resp.status_code not in (200, 204):
                print(f"    [{sku}] Inventory PUT failed: {resp.status_code} - {resp.text[:300]}")
                continue
            print(f"    [{sku}] Inventory item updated with new aspects")
            time.sleep(1)

            # Step 4: Update offer category
            offers = client.get_offers_by_sku(sku)
            if not offers:
                print(f"    [{sku}] No offers found")
                continue
            offer_id = offers[0].get("offerId")
            success = client.update_offer_category(offer_id, new_cat, listing_description=description)
            if success:
                # Step 5: Update local DB
                opt["aspects"] = {**opt.get("aspects", {}), **{k: v for k, v in aspect_patch.items()}}
                opt["categoryId"] = new_cat
                opt["category_id"] = new_cat
                conn.execute(
                    "UPDATE collected_products SET optimization=? WHERE sku=?",
                    (json.dumps(opt, ensure_ascii=False), sku)
                )
                conn.commit()
                print(f"    [{sku}] [OK] Fixed!")
            else:
                print(f"    [{sku}] [FAIL] Offer update failed")
            time.sleep(1)
        except Exception as e:
            print(f"    [{sku}] Error: {e}")

    # Update local DB for SKUs already fixed on eBay
    if not dry_run:
        print("\n--- Syncing DB for previously-fixed SKUs ---")
        for sku, cat in DB_ONLY_FIXES:
            row = conn.execute(
                "SELECT optimization FROM collected_products WHERE sku=?", (sku,)
            ).fetchone()
            if row and row["optimization"]:
                opt = json.loads(row["optimization"])
                if str(opt.get("categoryId", "")) != cat:
                    opt["categoryId"] = cat
                    opt["category_id"] = cat
                    conn.execute(
                        "UPDATE collected_products SET optimization=? WHERE sku=?",
                        (json.dumps(opt, ensure_ascii=False), sku)
                    )
                    print(f"  [{sku}] DB updated: cat → {cat}")
                else:
                    print(f"  [{sku}] DB already correct")
        conn.commit()

    conn.close()


if __name__ == "__main__":
    main()
