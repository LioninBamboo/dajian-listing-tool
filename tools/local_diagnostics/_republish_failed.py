"""Re-publish 2 products that failed due to non-leaf categories"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / 'ebay_collection.db'
sys.path.insert(0, str(ROOT))

from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
from src.clients.real_ebay_client import RealEbayClient

SKUS = ["N757P408833B", "N762P395711D"]
NEW_CATS = {"N757P408833B": "139849", "N762P395711D": "103431"}

oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
pm = EbayPolicyManager(oauth)
client = RealEbayClient(oauth, pm)

for sku in SKUS:
    print(f"\n=== {sku} → category {NEW_CATS[sku]} ===")
    
    # Delete existing unpublished offer
    offers = client.get_offers_by_sku(sku)
    if offers:
        for o in offers:
            oid = o.get("offerId")
            status = o.get("status")
            if status == "UNPUBLISHED":
                print(f"  Deleting unpublished offer {oid}...")
                client.delete_offer(oid)
                time.sleep(1)
            else:
                print(f"  Offer {oid} is {status} — skipping delete")
    
    # Update category in DB
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    row = conn.execute("SELECT optimization FROM collected_products WHERE sku=?", (sku,)).fetchone()
    if row and row[0]:
        opt = json.loads(row[0])
        opt["categoryId"] = NEW_CATS[sku]
        opt["category_id"] = NEW_CATS[sku]
        conn.execute("UPDATE collected_products SET optimization=?, status='READY' WHERE sku=?",
                     (json.dumps(opt, ensure_ascii=False), sku))
        conn.commit()
        print(f"  DB updated: cat={NEW_CATS[sku]}, status=READY")
    conn.close()

print("\nProducts reset to READY. Run batch_publish.py --sku N757P408833B,N762P395711D to re-publish.")
