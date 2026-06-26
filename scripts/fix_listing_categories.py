"""
Legacy category-fix script.

This script is kept for historical reference, but the preferred paths are:
1. `python scripts/repair_published_taxonomy.py`
2. `python scripts/audit_fix_active_listings.py`

The newer paths use current canonical taxonomy, plausibility checks, and
inventory-first revise flow. Prefer them unless you are investigating history.

Original behavior:

This script:
1. Identifies all published listings with wrong category IDs
2. For each listing, gets the eBay offer via SKU
3. Updates the offer with the correct category ID
4. Updates the local DB to match

Legacy usage:
    python scripts/fix_listing_categories.py          # Dry run (default)
    python scripts/fix_listing_categories.py --apply  # Actually apply fixes
"""

import sys, os, sqlite3, json, time, logging, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from src.services.ebay_auth import EbayOAuthService
from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_publisher import EbayPublisher
from sqlalchemy.orm.attributes import flag_modified

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ====== CATEGORY FIX MAPPING ======
# Format: old_category_id -> new_category_id
KNOWN_WRONG_CATEGORIES = {
    "177816": "107578",   # Bicycle Components → Dining Sets
    "177815": "38204",    # Bicycle Forks → Tables
    "25863":  "139849",   # non-leaf Patio Furniture → Patio Furniture Sets
    "25458":  "54235",    # non-leaf Dining Chairs → Chairs
    "183316": "103431",   # non-leaf Bar Stools → Bar Stools & Stools
    "63557":  "183322",   # non-leaf Sideboards → Sideboards & Buffets
    "181087": "79691",    # deprecated Coolers → Ice Chests & Coolers
}

# SKUs that are in wrong category 38208 (Sofas) but are actually other product types
# Format: SKU -> correct_category_id
MANUAL_FIXES = {
    # Shower Doors → 147147
    "W1675P307141": "147147",
    "W1675P338287": "147147",
    "W1675P338288": "147147",
    "W1675P288812": "147147",
    "W1675P288488": "147147",
    "W1675P288823": "147147",
    "W1675P288824": "147147",
    # Lawn Mowers → 260921
    "W465P369984": "260921",
    "W465P369979": "260921",
    # Welders
    "W5733P464934": "124822",  # MIG → 124822
    "W5733P464924": "124825",  # TIG → 124825
    "W5733P464943": "124822",  # MIG → 124822
    # Pool Covers/Domes → 181068
    "W3859P399054": "181068",
    "W3859P399055": "181068",
    "W3859P399056": "181068",
    "W3859P399057": "181068",
    "W3859P399058": "181068",
    # Golf Bag Organizer → 30109
    "W1422P289116": "30109",
    # Chandeliers/Lighting → 117503
    "W1340P279982": "117503",
    "W1340P266137": "117503",
    "W1340P266138": "117503",
    "W1340P279978": "117503",
    # Inflatable Water Park → 145979
    "W1677P457598": "145979",
    # Compost Spreader → 118869
    "W2286P432000": "118869",
    # Outdoor Lounges/Chairs → 79682
    "W640P443483": "79682",
    "TM000031AAD": "79682",
    "TM000033AAD": "79682",
    "TM000033AAE": "79682",
    "N715P404843D": "79682",
    "N715P404843E": "79682",
}

CATEGORY_NAMES = {
    "107578": "Dining Sets",
    "38204": "Tables",
    "139849": "Patio & Garden Furniture Sets",
    "54235": "Chairs",
    "103431": "Bar Stools & Stools",
    "183322": "Sideboards & Buffets",
    "79691": "Ice Chests & Coolers",
    "147147": "Shower Doors",
    "260921": "Lawn Mowers",
    "124822": "MIG Welders",
    "124825": "TIG Welders",
    "181068": "Pool Covers & Reels",
    "30109": "Golf Club Bags",
    "117503": "Chandeliers & Ceiling Fixtures",
    "145979": "Inflatable Bouncers",
    "118869": "Seeders & Spreaders",
    "79682": "Patio Chairs",
    "75671": "Wheelbarrows, Carts & Wagons",
}


def get_fixes(conn):
    """Get all published products that need category fixes."""
    cur = conn.cursor()
    rows = cur.execute(
        'SELECT sku, title, listing_id, optimization FROM collected_products WHERE status = ?',
        ('PUBLISHED',)
    ).fetchall()

    fixes = []
    for sku, title, listing_id, opt_json in rows:
        opt = json.loads(opt_json) if opt_json else {}
        current_cat = str(opt.get('categoryId', ''))

        new_cat = None
        reason = None

        # Check manual fixes first
        if sku in MANUAL_FIXES:
            new_cat = MANUAL_FIXES[sku]
            reason = "MANUAL_FIX"
        # Check known wrong categories
        elif current_cat in KNOWN_WRONG_CATEGORIES:
            new_cat = KNOWN_WRONG_CATEGORIES[current_cat]
            reason = "KNOWN_WRONG"

        if new_cat and new_cat != current_cat:
            fixes.append({
                'sku': sku,
                'title': (title or '')[:70],
                'listing_id': listing_id,
                'old_cat': current_cat,
                'new_cat': new_cat,
                'reason': reason,
            })

    return fixes


def apply_fix(ebay_client, conn, fix, dry_run=True):
    """Apply a single category fix to eBay and local DB."""
    sku = fix['sku']
    new_cat = fix['new_cat']
    old_cat = fix['old_cat']
    cat_name = CATEGORY_NAMES.get(new_cat, new_cat)

    if dry_run:
        print(f"  [DRY] {sku}: {old_cat} → {new_cat} ({cat_name})")
        return True

    # 1. Get eBay offer for this SKU
    offers = ebay_client.get_offers_by_sku(sku)
    if not offers:
        print(f"  [SKIP] {sku}: No offer found on eBay")
        return False

    offer = offers[0]
    offer_id = offer.get('offerId')
    if not offer_id:
        print(f"  [SKIP] {sku}: No offer ID")
        return False

    # 2. Update offer category on eBay
    success = ebay_client.update_offer_category(offer_id, new_cat)
    if not success:
        print(f"  [FAIL] {sku}: eBay update failed for offer {offer_id}")
        return False

    # 3. Update local DB
    cur = conn.cursor()
    row = cur.execute('SELECT optimization FROM collected_products WHERE sku = ?', (sku,)).fetchone()
    if row and row[0]:
        opt = json.loads(row[0])
        opt['categoryId'] = new_cat
        cur.execute(
            'UPDATE collected_products SET optimization = ? WHERE sku = ?',
            (json.dumps(opt), sku)
        )
        conn.commit()

    print(f"  [OK] {sku}: {old_cat} → {new_cat} ({cat_name}) | offer={offer_id}")
    return True


def main():
    parser = argparse.ArgumentParser(description='Fix wrong eBay listing categories')
    parser.add_argument('--apply', action='store_true', help='Actually apply fixes (default: dry run)')
    parser.add_argument('--sku', type=str, help='Fix only a specific SKU')
    args = parser.parse_args()

    dry_run = not args.apply

    conn = sqlite3.connect('ebay_collection.db')
    fixes = get_fixes(conn)

    if args.sku:
        fixes = [f for f in fixes if f['sku'] == args.sku]

    if not fixes:
        print("No category fixes needed!")
        return

    # Group by reason
    by_reason = {}
    for f in fixes:
        by_reason.setdefault(f['reason'], []).append(f)

    print(f"\n{'=' * 70}")
    print(f"Category Fix {'DRY RUN' if dry_run else 'APPLYING'}: {len(fixes)} listings")
    print(f"{'=' * 70}")

    for reason, items in sorted(by_reason.items()):
        print(f"\n--- {reason} ({len(items)} listings) ---")
        for f in items:
            cat_name = CATEGORY_NAMES.get(f['new_cat'], f['new_cat'])
            print(f"  {f['sku']}: {f['old_cat']} → {f['new_cat']} ({cat_name})")
            print(f"    Title: {f['title']}")

    if dry_run:
        print(f"\n{'=' * 70}")
        print(f"DRY RUN complete. Run with --apply to execute fixes.")
        print(f"{'=' * 70}")
        conn.close()
        return

    # Apply fixes
    print(f"\n{'=' * 70}")
    print(f"Applying {len(fixes)} fixes...")
    print(f"{'=' * 70}")

    oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
    from src.services.ebay_policy_manager import EbayPolicyManager
    policy_mgr = EbayPolicyManager(oauth)
    ebay_client = RealEbayClient(oauth, policy_mgr)

    success_count = 0
    fail_count = 0

    for i, fix in enumerate(fixes):
        print(f"\n[{i+1}/{len(fixes)}] Fixing {fix['sku']}...")
        ok = apply_fix(ebay_client, conn, fix, dry_run=False)
        if ok:
            success_count += 1
        else:
            fail_count += 1
        # Rate limiting - eBay API has limits
        time.sleep(1.5)

    conn.close()

    print(f"\n{'=' * 70}")
    print(f"DONE: {success_count} fixed, {fail_count} failed out of {len(fixes)} total")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    main()
