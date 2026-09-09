"""Audit published eBay listings for category, dimension, and pricing issues."""
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / 'ebay_collection.db'
sys.path.insert(0, str(ROOT))

from src.services.ebay_category_matcher import EbayCategoryMatcher

def audit():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute(
        'SELECT sku, title, listing_id, optimization, attributes, specs '
        'FROM collected_products WHERE status=? ORDER BY sku',
        ('PUBLISHED',)
    ).fetchall()
    
    print(f"=== Auditing {len(rows)} PUBLISHED listings ===\n")
    
    from src.services.ebay_auth import EbayOAuthService
    oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
    matcher = EbayCategoryMatcher(oauth)
    
    issues = {
        'wrong_category': [],
        'missing_dims': [],
        'wrong_dims': [],
        'no_listing_id': [],
        'title_too_long': [],
    }
    
    for row in rows:
        sku = row['sku']
        title = row['title'] or ''
        listing_id = row['listing_id']
        opt = json.loads(row['optimization']) if row['optimization'] else {}
        attrs = json.loads(row['attributes']) if row['attributes'] else {}
        specs = json.loads(row['specs']) if row['specs'] else {}
        
        opt_title = opt.get('title', title)
        
        # 1. Check listing ID
        if not listing_id:
            issues['no_listing_id'].append(sku)
            continue
        
        # 2. Check category using current matcher logic
        published_cat = opt.get('category_id') or opt.get('categoryId')
        if opt_title:
            # Use fallback only (no API call) to check keyword match
            new_cat, new_name = matcher._fallback_category(opt_title, '')
            if published_cat and new_cat != str(published_cat) and new_cat != '38208':
                # Only flag if keyword match gives a specific different result
                issues['wrong_category'].append({
                    'sku': sku,
                    'listing_id': listing_id,
                    'title': opt_title[:70],
                    'published_cat': published_cat,
                    'expected_cat': new_cat,
                    'expected_name': new_name,
                })
        
        # 3. Check dimensions in aspects
        aspects = opt.get('aspects', {})
        dim_keys = ['Item Length', 'Item Width', 'Item Height', 'Item Weight']
        missing = []
        see_desc = []
        for dk in dim_keys:
            val = aspects.get(dk, [''])[0] if isinstance(aspects.get(dk), list) else aspects.get(dk, '')
            if not val:
                missing.append(dk)
            elif 'see description' in str(val).lower():
                see_desc.append(dk)
        
        if missing:
            issues['missing_dims'].append({
                'sku': sku, 'listing_id': listing_id, 'missing': missing
            })
        
        # 4. Check for guardrail height bug (height < 15 inches for beds/bunk beds)
        height_val = aspects.get('Item Height', [''])[0] if isinstance(aspects.get('Item Height'), list) else aspects.get('Item Height', '')
        if height_val and 'in' in str(height_val):
            try:
                h = float(str(height_val).replace('in', '').replace('"', '').strip())
                is_bed = any(kw in (opt_title or '').lower() for kw in ['bunk bed', 'loft bed', 'bed frame', 'daybed'])
                if is_bed and h < 20:
                    # Suspiciously low height for a bed
                    issues['wrong_dims'].append({
                        'sku': sku, 'listing_id': listing_id,
                        'title': opt_title[:60],
                        'height': height_val,
                        'reason': 'Bed height too low — likely guardrail instead of assembled height'
                    })
            except ValueError:
                pass
        
        # 5. Title length
        if opt_title and len(opt_title) > 80:
            issues['title_too_long'].append({
                'sku': sku, 'chars': len(opt_title)
            })
    
    # Print results
    print(f"\n{'='*60}")
    print("AUDIT RESULTS")
    print(f"{'='*60}")
    
    if issues['wrong_category']:
        print(f"\n🔴 WRONG CATEGORY: {len(issues['wrong_category'])} listings")
        for i in issues['wrong_category']:
            print(f"  {i['sku']} (eBay #{i['listing_id']})")
            print(f"    Title: {i['title']}")
            print(f"    Published: {i['published_cat']} -> Should be: {i['expected_cat']} ({i['expected_name']})")
    else:
        print("\n✅ Categories: All correct")
    
    if issues['wrong_dims']:
        print(f"\n🔴 WRONG DIMENSIONS: {len(issues['wrong_dims'])} listings")
        for i in issues['wrong_dims']:
            print(f"  {i['sku']} (eBay #{i['listing_id']})")
            print(f"    {i['title']}")
            print(f"    Height={i['height']} — {i['reason']}")
    else:
        print("\n✅ Dimensions: No suspicious heights detected")
    
    if issues['missing_dims']:
        count = len(issues['missing_dims'])
        print(f"\n⚠️  MISSING DIMENSIONS: {count} listings")
        for i in issues['missing_dims'][:20]:
            print(f"  {i['sku']} — missing: {', '.join(i['missing'])}")
        if count > 20:
            print(f"  ... and {count-20} more")
    else:
        print("\n✅ All listings have dimension aspects")
    
    if issues['no_listing_id']:
        print(f"\n⚠️  NO LISTING ID: {len(issues['no_listing_id'])} products marked PUBLISHED without listing ID")
        for s in issues['no_listing_id'][:10]:
            print(f"  {s}")
    
    # Summary
    total_issues = sum(len(v) for v in issues.values())
    print(f"\n{'='*60}")
    print(f"TOTAL: {total_issues} issues found across {len(rows)} published listings")
    print(f"{'='*60}")
    
    conn.close()
    return issues

if __name__ == '__main__':
    audit()
