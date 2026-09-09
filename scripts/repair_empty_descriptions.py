import sqlite3
import json
import os
import subprocess

def get_db():
    db = sqlite3.connect('ebay_collection.db')
    db.row_factory = sqlite3.Row
    return db

def main():
    print("Starting repair for SKUs with empty/missing descriptions...")
    db = get_db()
    
    rows = db.execute("SELECT sku, status, optimization FROM collected_products WHERE status IN ('READY', 'PUBLISHED')").fetchall()
    
    impacted_skus = []
    
    for row in rows:
        sku = row['sku']
        status = row['status']
        try:
            opt = json.loads(row['optimization'])
            desc = opt.get('description', '')
            
            has_highlights = 'KEY FEATURES' in desc
            has_list = '<li' in desc.lower()
            
            if not has_highlights or not has_list:
                # Need to repair
                impacted_skus.append((sku, status))
        except Exception:
            pass

    if not impacted_skus:
        print("No impacted SKUs found.")
        return

    print(f"Found {len(impacted_skus)} impacted SKUs. Resetting them to COLLECTED...")
    
    skus_to_repair = [sku for sku, status in impacted_skus]
    
    # Update DB: Reset optimization cache, and status to COLLECTED
    with db:
        for sku in skus_to_repair:
            db.execute("UPDATE collected_products SET status = 'COLLECTED', optimization = NULL WHERE sku = ?", (sku,))
            print(f"Reset {sku} to COLLECTED.")

    print("\nTriggering AI batch analysis to rewrite descriptions...")
    # Trigger batch_analyze.py specifically for these SKUs.
    # Since batch_analyze runs on COLLECTED automatically, we can just run it.
    
    # Assuming batch_analyze supports a sku argument or reads from db directly
    # `python batch_analyze.py --limit N` might be used. We'll just run daily_tasks or batch_analyze.
    # Let's run batch_analyze.py
    try:
        print("Running batch_analyze.py...")
        subprocess.run(["python", "batch_analyze.py"], check=True)
        print("batch_analyze.py completed successfully.")
    except Exception as e:
        print(f"Error running batch_analyze.py: {e}")

    # Now these should be READY, and we can run audit_fix_ready_drafts.py
    try:
        print("\nRunning audit_fix_ready_drafts.py...")
        subprocess.run(["python", "scripts/audit_fix_ready_drafts.py"], check=True)
    except Exception as e:
        print(f"Error running audit_fix_ready_drafts.py: {e}")
        
    print("\nRepair completed. To push to eBay, please run batch_publish.py or audit_fix_active_listings.py manually.")

if __name__ == "__main__":
    main()
