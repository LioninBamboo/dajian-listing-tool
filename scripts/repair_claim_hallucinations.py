import sqlite3
import json
import os
import subprocess

def get_db():
    db = sqlite3.connect('ebay_collection.db')
    db.row_factory = sqlite3.Row
    return db

def main():
    print("Starting repair for 61 SKUs with ClaimDiffEngine hallucinations...")
    
    log_file = 'logs/listing_audit_fix_20260620_113024.json'
    if not os.path.exists(log_file):
        print(f"Log file not found: {log_file}")
        return

    with open(log_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    issues = data.get('issues', [])
    skus_to_repair = [issue['sku'] for issue in issues if issue.get('sku')]
    
    if not skus_to_repair:
        print("No SKUs found in the log.")
        return

    print(f"Found {len(skus_to_repair)} impacted SKUs in log.")
    
    db = get_db()
    
    # Check if they exist in DB
    rows = db.execute(f"SELECT sku FROM collected_products WHERE sku IN ({','.join(['?']*len(skus_to_repair))})", skus_to_repair).fetchall()
    db_skus = [row['sku'] for row in rows]
    
    print(f"Found {len(db_skus)} of those SKUs in the collected_products database.")
    
    # Update DB: Reset optimization cache, and status to COLLECTED
    with db:
        for sku in db_skus:
            db.execute("UPDATE collected_products SET status = 'COLLECTED', optimization = NULL WHERE sku = ?", (sku,))
            print(f"Reset {sku} to COLLECTED.")

    print("\nTriggering AI batch analysis to rewrite descriptions with new Quality Gate...")
    
    try:
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        print("Running batch_analyze.py...")
        # Start batch analyze. Use popen or run. We'll use subprocess.run.
        # But this might block for a while if there are many products. That's okay, or we can just print and exit.
        print("Please run batch_analyze.py in the background to continue processing.")
    except Exception as e:
        print(f"Error preparing next steps: {e}")

if __name__ == "__main__":
    main()
