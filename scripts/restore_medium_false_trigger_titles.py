"""Restore 3 titles that a MEDIUM-noise false trigger rewrote. Title only:
the live description/aspects were correctly fixed and must be carried over
untouched. Backups for 2 of the 3 were overwritten by the 12:30 scheduled run,
so the pre-push titles come from the v1+v2 preview reports (both agree)."""
import sys, os, json
sys.path.insert(0, r"C:\Users\poonx\Dajian_Listing_Tool")
os.chdir(r"C:\Users\poonx\Dajian_Listing_Tool")
from dotenv import load_dotenv; load_dotenv('.env')
import importlib.util
spec = importlib.util.spec_from_file_location('afal', 'scripts/audit_fix_active_listings.py')
afal = importlib.util.module_from_spec(spec); sys.modules['afal'] = afal; spec.loader.exec_module(afal)
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
from src.clients.real_ebay_client import RealEbayClient

RESTORE = {
 'LP000677AAE-1': 'Full Size Murphy Bed with Large Drawers Storage Cabinet Wall Bed 77x53.5x43.4 in',
 'LP000678AAF-1': 'Queen Murphy Bed with Large Drawer & 80.7 x 62.8 x 44 in Green Murphy Wall Bed',
 'LP000677AAF-1': 'Full Size Murphy Bed with Large Drawers Storage Cabinet Wall Bed 77x53.5x43.4 in',
}
APPLY = '--apply' in sys.argv
oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT','PRODUCTION'))
client = RealEbayClient(oauth, EbayPolicyManager(oauth))

for sku, want in RESTORE.items():
    inv = client.get_inventory_item(sku) or {}
    prod = inv.get('product') or {}
    cur_title = prod.get('title') or ''
    cur_desc = prod.get('description') or ''
    cur_asp = prod.get('aspects') or {}
    print(f"=== {sku}")
    print(f"  now : ({len(cur_title)}) {cur_title}")
    print(f"  want: ({len(want)}) {want}")
    print(f"  desc unchanged: {len(cur_desc)} chars | aspects: {len(cur_asp)} keys")
    if cur_title == want:
        print("  -> already correct, skip"); continue
    if not APPLY:
        print("  -> DRY RUN (no write)"); continue
    afal._put_inventory_product_only(client, sku, want, cur_desc, cur_asp)
    after = ((client.get_inventory_item(sku) or {}).get('product') or {}).get('title') or ''
    print(f"  -> APPLIED, live now: {after}")
    print(f"  -> match: {after == want}")
