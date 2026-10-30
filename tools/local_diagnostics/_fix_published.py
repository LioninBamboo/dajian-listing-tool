"""
Analyze audit findings and fix truly wrong published listings on eBay.
Focuses on CATEGORY and DIMENSION errors only.
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
from src.services.ebay_category_matcher import EbayCategoryMatcher
from src.services.ebay_policy_manager import EbayPolicyManager
from src.clients.real_ebay_client import RealEbayClient
from src.utils.dimension_helpers import find_dimension, populate_dimension_aspects
from src.utils.ebay_quantity import coerce_nonnegative_quantity

# ──────────────────────────────────────────────────────────────
#  TRUE category fixes – manually reviewed, only clear errors
# ──────────────────────────────────────────────────────────────
# Format: (sku, current_wrong_cat, correct_cat, reason)
CATEGORY_FIXES = [
    # Bar stools published as Dining Sets
    ("W3916P426287", "177816", "183316", "Bar Stools, not Dining Sets"),

    # Dining Chairs published as Dining Sets
    ("N759P307032D", "177816", "25458", "Dining Chair set, not Dining Sets"),
    ("W1143141068", "177816", "25458", "Dining Chair, not Dining Sets"),

    # Floor beds published as generic Sofas
    ("W504P411200", "38208", "175758", "Montessori Floor Bed → Bed Frames"),
    ("W5305P429646", "38208", "175758", "House Floor Bed → Bed Frames"),

    # Armchairs/Chairs published as Bed Frames
    ("W2989P426363", "175758", "54235", "Armchair, not Bed Frame"),
    ("W3118P404121", "175758", "54235", "Task Chair, not Bed Frame"),
    ("W3188P400934", "175758", "54235", "Barrel Chair, not Bed Frame"),

    # Kids table published as Dining Sets
    ("W808P362279", "177816", "66756", "Kids Table → Kids Tables"),

    # Accent chairs category confusion
    ("W2105P210243", "118218", "54235", "Accent Armchair → Chairs (better match)"),

    # Pet treadmill in wrong category
    ("W215P296966", "175750", "46283", "Pet Treadmill → Pet Supplies"),

    # Coolers published as generic Sofas/wrong
    ("W5633P453705", "181087", "181087", None),  # Keep - correct for coolers
    ("W5633P453793", "181087", "181087", None),  # Keep
    ("W5633P453796", "181087", "181087", None),  # Keep
    ("W5633P453797", "38208", "181087", "Hard Cooler should be in Coolers"),
]

# Filter out None (no-fix-needed) entries
CATEGORY_FIXES = [(s, o, n, r) for s, o, n, r in CATEGORY_FIXES if r is not None]

# ──────────────────────────────────────────────────────────────
#  Dimension fixes – beds with wrong height
# ──────────────────────────────────────────────────────────────
DIMENSION_FIX_SKUS = [
    "LP000138AAE", "LP000139AAE", "LP000139AAN",
    "N708P263021N-1", "N708P263022E-1", "N708P263022N-1",
    "N709P241723P", "N709P241723P-1",
    "W2580S00074", "W2593S00041",
]


def fix_categories(conn, ebay_client, dry_run=True):
    """Fix wrong categories on published listings."""
    fixed = 0
    for sku, old_cat, new_cat, reason in CATEGORY_FIXES:
        row = conn.execute(
            "SELECT optimization, listing_id FROM collected_products WHERE sku=?",
            (sku,)
        ).fetchone()
        if not row:
            logging.warning(f"  [{sku}] Not found in DB")
            continue

        opt = json.loads(row[0]) if row[0] else {}
        listing_id = row[1]

        current = opt.get("categoryId") or opt.get("category_id", "")
        if str(current) == str(new_cat):
            logging.info(f"  [{sku}] Already correct ({new_cat})")
            continue

        logging.info(f"  [{sku}] #{listing_id}: {old_cat} → {new_cat} ({reason})")

        if dry_run:
            logging.info(f"    DRY: Would update category to {new_cat}")
            fixed += 1
            continue

        # Step 1: Get offers
        try:
            offers = ebay_client.get_offers_by_sku(sku)
            if not offers:
                logging.warning(f"    [{sku}] No offers found on eBay")
                continue
            offer_id = offers[0].get("offerId")

            # Step 2: Update offer category
            desc = opt.get("description", "")
            success = ebay_client.update_offer_category(offer_id, new_cat, listing_description=desc)
            if success:
                # Step 3: Update local DB
                opt["categoryId"] = new_cat
                opt["category_id"] = new_cat
                conn.execute(
                    "UPDATE collected_products SET optimization=? WHERE sku=?",
                    (json.dumps(opt, ensure_ascii=False), sku)
                )
                conn.commit()
                logging.info(f"    [OK] Category updated to {new_cat}")
                fixed += 1
            else:
                logging.error(f"    [FAIL] Could not update category")
            time.sleep(1)  # Rate limit
        except Exception as e:
            logging.error(f"    [{sku}] Error: {e}")

    return fixed


def fix_dimensions(conn, ebay_client, dry_run=True):
    """Fix beds with wrong height (guardrail instead of assembled)."""
    fixed = 0
    for sku in DIMENSION_FIX_SKUS:
        row = conn.execute(
            "SELECT optimization, attributes, specs, listing_id FROM collected_products WHERE sku=?",
            (sku,)
        ).fetchone()
        if not row:
            logging.warning(f"  [{sku}] Not found in DB")
            continue

        opt = json.loads(row[0]) if row[0] else {}
        attrs = json.loads(row[1]) if row[1] else {}
        specs = json.loads(row[2]) if row[2] else {}
        listing_id = row[3]
        aspects = opt.get("aspects", {})

        # Get current height
        old_height = aspects.get("Item Height", [""])[0] if isinstance(aspects.get("Item Height"), list) else aspects.get("Item Height", "")

        # Try to get correct assembled height from source data
        correct_height = find_dimension(attrs, "Height")
        if not correct_height:
            logging.warning(f"  [{sku}] No assembled height in source data")
            continue

        correct_str = f"{correct_height} in"
        if old_height == correct_str:
            logging.info(f"  [{sku}] Height already correct: {correct_str}")
            continue

        logging.info(f"  [{sku}] #{listing_id}: Height {old_height} → {correct_str}")

        if dry_run:
            logging.info(f"    DRY: Would fix height to {correct_str}")
            fixed += 1
            continue

        # Fix all dimensions using populate_dimension_aspects
        populate_dimension_aspects(aspects, attrs, specs)

        # Update inventory item
        title = opt.get("title", "")[:80]
        description = opt.get("description", "")
        images = json.loads(row[1]) if False else []  # Will use existing EPS URLs

        try:
            # Get existing inventory item to preserve images
            inv = ebay_client.get_inventory_item(sku)
            if inv:
                existing_images = inv.get("product", {}).get("imageUrls", [])
                existing_quantity = coerce_nonnegative_quantity(
                    ((inv.get("availability") or {}).get("shipToLocationAvailability") or {}).get("quantity"),
                    fallback=1,
                )
            else:
                existing_images = []
                existing_quantity = 1

            inv_product = {
                "title": title,
                "description": description,
                "image_urls": existing_images[:12],
                "price": 99.99,
                "quantity": existing_quantity,
                "condition": "NEW",
                "aspects": aspects,
            }
            ebay_client.create_or_replace_inventory_item(sku=sku, product=inv_product)

            # Update local DB
            opt["aspects"] = aspects
            conn.execute(
                "UPDATE collected_products SET optimization=? WHERE sku=?",
                (json.dumps(opt, ensure_ascii=False), sku)
            )
            conn.commit()
            logging.info(f"    [OK] Dimensions fixed")
            fixed += 1
            time.sleep(1)
        except Exception as e:
            logging.error(f"    [{sku}] Error: {e}")

    return fixed


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--apply", action="store_true", help="Actually apply fixes")
    args = parser.parse_args()

    dry_run = not args.apply

    print("=" * 60)
    print(f"FIX PUBLISHED LISTINGS {'(DRY RUN)' if dry_run else '*** LIVE ***'}")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    policy_mgr = EbayPolicyManager(oauth)
    ebay_client = RealEbayClient(oauth, policy_mgr)

    print(f"\n--- CATEGORY FIXES ({len(CATEGORY_FIXES)} listings) ---")
    cat_fixed = fix_categories(conn, ebay_client, dry_run)

    print(f"\n--- DIMENSION FIXES ({len(DIMENSION_FIX_SKUS)} listings) ---")
    dim_fixed = fix_dimensions(conn, ebay_client, dry_run)

    print(f"\n{'=' * 60}")
    print(f"RESULTS: {cat_fixed} category fixes, {dim_fixed} dimension fixes")
    if dry_run:
        print("Run with --apply to actually fix these on eBay")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
