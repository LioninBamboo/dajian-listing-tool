import sqlite3
import json
import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
from src.clients.real_ebay_client import create_real_ebay_client
from src.utils.title_sanitizer import normalize_listing_title_for_ebay

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    logger.info("Starting targeted text update for READY items...")
    
    # Init eBay client
    ebay_client = create_real_ebay_client("PRODUCTION")
    
    db = sqlite3.connect('ebay_collection.db')
    db.row_factory = sqlite3.Row
    cursor = db.cursor()
    
    # Get all READY items
    cursor.execute("SELECT sku, optimization as opt FROM collected_products WHERE status = 'READY'")
    items = cursor.fetchall()
    
    logger.info(f"Found {len(items)} READY items to update.")
    
    success_count = 0
    fail_count = 0
    
    for row in items:
        sku = row['sku']
        try:
            opt = json.loads(row['opt']) if row['opt'] else {}
            if not opt or 'description' not in opt:
                logger.warning(f"[{sku}] No optimized description found in DB. Skipping.")
                continue
                
            new_title = opt.get('title', '')
            new_desc = opt.get('description', '')
            
            if not new_title or not new_desc:
                logger.warning(f"[{sku}] Missing title or description in DB. Skipping.")
                continue
                
            new_title, _ = normalize_listing_title_for_ebay(new_title)
                
            # 1. Fetch current inventory from eBay
            inventory = ebay_client.get_inventory_item(sku)
            if not inventory:
                logger.warning(f"[{sku}] Not found on eBay (or API error). Skipping.")
                fail_count += 1
                continue
                
            # 2. Update ONLY product text fields
            product = inventory.get("product", {})
            product["title"] = new_title
            product["description"] = new_desc
            
            # Optionally update aspects without touching images
            if 'aspects' in opt and opt['aspects']:
                product["aspects"] = opt['aspects']
                
            # Clean up invalid weight from eBay's existing payload
            if "packageWeightAndSize" in inventory:
                weight = inventory["packageWeightAndSize"].get("weight", {})
                if weight.get("value") in (0, 0.0, "0", "0.0"):
                    del inventory["packageWeightAndSize"]["weight"]
                    # If empty, remove packageWeightAndSize entirely
                    if not inventory["packageWeightAndSize"]:
                        del inventory["packageWeightAndSize"]
                        
            inventory["product"] = product
            
            # 3. Push back to eBay using raw request to preserve full payload
            logger.info(f"[{sku}] Updating text on eBay...")
            
            token = ebay_client.oauth.get_valid_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Language": "en-US",
                "Accept": "application/json",
            }
            url = f"{ebay_client.base_url}/sell/inventory/v1/inventory_item/{sku}"
            response = ebay_client.session.put(url, headers=headers, json=inventory, timeout=60)
            
            if response.status_code in (200, 204):
                logger.info(f"[{sku}] SUCCESS.")
                cursor.execute("UPDATE collected_products SET status = 'PUBLISHED' WHERE sku = ?", (sku,))
                db.commit()
                success_count += 1
            else:
                logger.error(f"[{sku}] FAILED to update on eBay: HTTP {response.status_code} {response.text}")
                fail_count += 1
                
        except Exception as e:
            logger.error(f"[{sku}] Error processing: {e}")
            fail_count += 1
            
    logger.info(f"Update complete! Success: {success_count}, Failed: {fail_count}")

if __name__ == "__main__":
    main()
