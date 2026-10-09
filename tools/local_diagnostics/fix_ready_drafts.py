import os, sys, json, re
from pathlib import Path
from dotenv import load_dotenv
import sqlite3

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from src.clients.dajian_client import DaJianClient

def clean_html_dimensions(html_str):
    if not html_str: return html_str
    
    # Replace the Overall Dimensions row
    dim_pattern = re.compile(r'<td[^>]*>Overall Dimensions \(L×W×H\)</td>\s*<td[^>]*>[^<]+</td>', re.IGNORECASE)
    html_str = dim_pattern.sub('<td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72;width:40%">Overall Dimensions (L×W×H)</td><td style="padding:10px;border-bottom:1px solid #e0e0e0;font-weight:500">Please refer to the dimension image in the gallery for exact measurements.</td>', html_str)
    
    # We can also replace Weight row if we want to be safe
    wt_pattern = re.compile(r'<td[^>]*>Weight</td>\s*<td[^>]*>[^<]+</td>', re.IGNORECASE)
    html_str = wt_pattern.sub('<td style="padding:10px;border-bottom:1px solid #e0e0e0;color:#636e72">Weight</td><td style="padding:10px;border-bottom:1px solid #e0e0e0;font-weight:500">Please refer to the dimension image in the gallery.</td>', html_str)
    
    return html_str

def main():
    client = DaJianClient(os.getenv('DAJIAN_API_KEY'), os.getenv('DAJIAN_API_SECRET'))
    
    conn = sqlite3.connect(str(PROJECT_ROOT / "ebay_collection.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM collected_products WHERE status='READY'").fetchall()
    
    cur = conn.cursor()
    
    fixed_count = 0
    for row in rows:
        sku = row['sku']
        opt = json.loads(row['optimization']) if row['optimization'] else {}
        
        detail = client.get_product_detail_by_sku(sku)
        if not detail:
            print(f"[{sku}] Could not fetch API details, skipping.")
            continue
            
        aspects = opt.get('aspects', {})
        
        # Determine if we have real assembled dimensions
        has_real_dims = detail.get('assembledLength') not in [None, 'Not Applicable', '', '0']
        has_real_weight = detail.get('weight') not in [None, 'Not Applicable', '', '0'] or detail.get('assembledWeight') not in [None, 'Not Applicable', '', '0']
        
        changed = False
        
        # If no real dimensions, remove hallucinated aspects and patch description
        if not has_real_dims or not has_real_weight:
            keys_to_remove = ['Item Length', 'Item Width', 'Item Height', 'Item Weight', 'Assembly Required']
            for k in keys_to_remove:
                if k in aspects:
                    del aspects[k]
                    changed = True
                    
            old_desc = opt.get('description', '')
            new_desc = clean_html_dimensions(old_desc)
            if old_desc != new_desc:
                opt['description'] = new_desc
                changed = True
                
        if changed:
            cur.execute("UPDATE collected_products SET optimization=? WHERE sku=?", (json.dumps(opt, ensure_ascii=False), sku))
            fixed_count += 1
            print(f"[{sku}] Fixed missing/hallucinated dimensions.")
            
    conn.commit()
    conn.close()
    
    print(f"\nSuccessfully fixed {fixed_count} products.")

if __name__ == '__main__':
    main()
