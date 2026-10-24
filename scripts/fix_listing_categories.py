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
from src.utils.approved_category import (
    blocks_automated_category_change,
    locked_category_write_error,
)
from src.utils.category_decision import (
    explicit_override_target_allowed,
    gate_publish_category_choice,
)
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


def print_readback_table(target_skus, confirmed_skus, unfinished_skus):
    print("readback: target / confirmed / unfinished")
    print(f"  target     ({len(target_skus)}): {', '.join(target_skus) or '-'}")
    print(f"  confirmed  ({len(confirmed_skus)}): {', '.join(confirmed_skus) or '-'}")
    print(f"  unfinished ({len(unfinished_skus)}): {', '.join(unfinished_skus) or '-'}")


def get_fixes(conn, include_locked=False):
    """Get all published products that need category fixes."""
    cur = conn.cursor()
    rows = cur.execute(
        'SELECT sku, title, listing_id, optimization FROM collected_products WHERE status = ?',
        ('PUBLISHED',)
    ).fetchall()

    fixes = []
    for sku, title, listing_id, opt_json in rows:
        opt = json.loads(opt_json) if opt_json else {}
        if blocks_automated_category_change(opt) and not include_locked:
            continue
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
                'optimization': opt,
            })

    return fixes


def apply_fix(
    ebay_client,
    conn,
    fix,
    dry_run=True,
    override_category_lock=False,
    explicit_operator_id=None,
):
    """Apply a single category fix to eBay and local DB.

    The hardcoded KNOWN_WRONG / MANUAL table always goes through the gate
    and the lock. ``--override-category-lock`` may write only when the
    operator also passed ``--sku`` and ``--category-id``.
    """
    sku = fix['sku']
    new_cat = fix['new_cat']
    old_cat = fix['old_cat']
    cat_name = CATEGORY_NAMES.get(new_cat, new_cat)
    opt = fix.get('optimization') or {}
    if not opt:
        row = conn.execute(
            'SELECT optimization FROM collected_products WHERE sku = ?', (sku,)
        ).fetchone()
        opt = json.loads(row[0]) if row and row[0] else {}

    explicit = str(explicit_operator_id or "").strip()
    locked = blocks_automated_category_change(opt)
    override_ok = bool(override_category_lock and explicit and locked)

    lock_error = locked_category_write_error(opt, override=override_ok)
    if lock_error:
        print(f"  [LOCK] {sku}: {lock_error}")
        fix['skip_reason'] = 'category_locked'
        return False

    title = fix.get('title') or ''
    if str(new_cat or '').strip() == str(old_cat or '').strip():
        pass
    elif override_ok:
        if str(new_cat or "").strip() != explicit:
            print(f"  [REPORT] {sku}: override writes only the explicit operator id")
            fix['skip_reason'] = 'override_requires_explicit_id'
            return False
        ok, reason = explicit_override_target_allowed(new_cat, title, stored=old_cat)
        if not ok:
            print(f"  [REPORT] {sku}: {reason} ({old_cat} → {new_cat})")
            fix['skip_reason'] = reason
            return False
    else:
        gated, action, hold_err = gate_publish_category_choice(
            old_cat,
            new_cat,
            title,
            'PUBLISHED',
            reason='publish_candidate',
            optimization=opt,
        )
        if hold_err or action != 'remap':
            print(f"  [REPORT] {sku}: would_remap_report_only ({old_cat} → {new_cat})")
            fix['skip_reason'] = 'would_remap_report_only'
            return False

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


def operator_override_fixes(conn, sku, category_id):
    """Build the single operator ``--sku`` / ``--category-id`` row."""
    explicit = str(category_id or "").strip()
    target_sku = str(sku or "").strip()
    if not target_sku or not explicit:
        return []
    row = conn.execute(
        "SELECT sku, title, listing_id, optimization FROM collected_products WHERE sku = ?",
        (target_sku,),
    ).fetchone()
    if not row:
        return []
    sku_r, title, listing_id, opt_json = row
    opt = json.loads(opt_json) if opt_json else {}
    current = str(opt.get("categoryId") or "")
    return [{
        "sku": sku_r,
        "title": (title or "")[:70],
        "listing_id": listing_id,
        "old_cat": current,
        "new_cat": explicit,
        "reason": "OPERATOR_OVERRIDE",
        "optimization": opt,
    }]


def validate_fix_cli_scope(*, override=False, sku=None, category_id=None) -> str:
    """Refuse bulk override / bulk ``--category-id``. Empty string = ok."""
    if category_id and not sku:
        return "--category-id requires --sku (never a bulk remap)"
    if override and not (sku and category_id):
        return "--override-category-lock requires both --sku and --category-id"
    return ""


def scope_category_fixes(fixes, sku=None, category_id=None, *, override=False):
    """Apply ``--category-id`` only to the ``--sku`` row; never bulk."""
    err = validate_fix_cli_scope(override=override, sku=sku, category_id=category_id)
    if err:
        raise ValueError(err)
    scoped = [dict(item) for item in fixes]
    if sku:
        scoped = [item for item in scoped if item.get("sku") == sku]
    if category_id:
        explicit = str(category_id).strip()
        for item in scoped:
            item["new_cat"] = explicit
    return scoped


def main():
    parser = argparse.ArgumentParser(description='Fix wrong eBay listing categories')
    parser.add_argument('--apply', action='store_true', help='Actually apply fixes (default: dry run)')
    parser.add_argument('--sku', type=str, help='Fix only a specific SKU')
    parser.add_argument(
        '--override-category-lock',
        action='store_true',
        help='Allow remapping a listing whose optimization.categoryApproved flag is set',
    )
    parser.add_argument(
        '--category-id',
        dest='category_id',
        help='Explicit override target id (never a matcher suggestion)',
    )
    args = parser.parse_args()
    scope_err = validate_fix_cli_scope(
        override=args.override_category_lock,
        sku=args.sku,
        category_id=args.category_id,
    )
    if scope_err:
        parser.error(scope_err)

    dry_run = not args.apply

    conn = sqlite3.connect('ebay_collection.db')
    if args.override_category_lock:
        # Operator path only: never expand to the hardcoded tables.
        fixes = operator_override_fixes(conn, args.sku, args.category_id)
    else:
        fixes = scope_category_fixes(
            get_fixes(conn, include_locked=False),
            sku=args.sku,
            category_id=args.category_id,
            override=False,
        )

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
        target_skus = [f['sku'] for f in fixes]
        confirmed_skus = []
        unfinished_skus = []
        for item in fixes:
            ok = apply_fix(
                None,
                conn,
                item,
                dry_run=True,
                override_category_lock=args.override_category_lock,
                explicit_operator_id=args.category_id,
            )
            if ok:
                confirmed_skus.append(item['sku'])
            else:
                unfinished_skus.append(item['sku'])
        print(f"\n{'=' * 70}")
        print(f"DRY RUN complete. Run with --apply to execute fixes.")
        print(f"{'=' * 70}")
        print_readback_table(target_skus, confirmed_skus, unfinished_skus)
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
    confirmed_skus = []
    unfinished_skus = []

    for i, fix in enumerate(fixes):
        print(f"\n[{i+1}/{len(fixes)}] Fixing {fix['sku']}...")
        ok = apply_fix(
            ebay_client,
            conn,
            fix,
            dry_run=False,
            override_category_lock=args.override_category_lock,
            explicit_operator_id=args.category_id,
        )
        if ok:
            success_count += 1
            confirmed_skus.append(fix['sku'])
        else:
            fail_count += 1
            unfinished_skus.append(fix['sku'])
        # Rate limiting - eBay API has limits
        time.sleep(1.5)

    conn.close()

    print(f"\n{'=' * 70}")
    print(f"DONE: {success_count} fixed, {fail_count} failed out of {len(fixes)} total")
    print(f"{'=' * 70}")
    print_readback_table([f['sku'] for f in fixes], confirmed_skus, unfinished_skus)


if __name__ == '__main__':
    main()
