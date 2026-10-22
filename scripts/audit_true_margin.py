#!/usr/bin/env python3
"""Audit TRUE margin: real landed cost (incl. GigaCloud dropship shippingFee)
vs the live eBay price. pilot_collect_garden hardcoded shipping=0, so any SKU
collected that way had its cost understated by the freight and was priced too low.

Outputs, per published SKU: live eBay price, real cost, real margin. Flags those
below a floor. Writes reprice_targets.json = [{sku, listing_id, live, real_cost,
new_price}] for the fix pass.
"""
from __future__ import annotations
import os, sys, sqlite3, json, time, re
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
import requests
from src.clients.dajian_client import DaJianClient
from src.services.pricing_engine import PricingEngine
from src.services.ebay_auth import EbayOAuthService

TARGET_GROSS = float(os.getenv("TARGET_GROSS", "0.35"))  # markup target over true cost
FLOOR_GROSS = 0.30

def main():
    db = sqlite3.connect('ebay_collection.db'); db.row_factory = sqlite3.Row
    rows = db.execute("SELECT sku,listing_id,specs,attributes FROM collected_products "
                      "WHERE status='PUBLISHED' AND listing_id!=''").fetchall()
    skus = [r['sku'] for r in rows]
    meta = {r['sku']: r for r in rows}
    dj = DaJianClient(os.getenv('DAJIAN_API_KEY'), os.getenv('DAJIAN_API_SECRET'))

    # batch Dajian prices (product_price + shippingFee), paced
    price_ship = {}
    CH = 30
    for i in range(0, len(skus), CH):
        chunk = skus[i:i+CH]
        for attempt in range(5):
            try:
                pr = dj.get_product_prices(chunk) or []
                break
            except Exception:
                pr = []; time.sleep(8*(attempt+1))
        for p in pr:
            if isinstance(p, dict) and p.get('sku'):
                price_ship[p['sku']] = (
                    float(p.get('price') or 0),
                    float(p.get('shippingFee') or 0),
                )
        time.sleep(5)
        print(f"  fetched prices {len(price_ship)}/{len(skus)}", flush=True)

    tok = EbayOAuthService(environment='PRODUCTION').get_valid_token()
    h = {'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'}

    reprice = []; margins = []; missing = 0
    for r in rows:
        sku = r['sku']
        pp, sf = price_ship.get(sku, (0, 0))
        if not pp:
            missing += 1; continue
        specs = json.loads(r['specs'] or '{}'); attrs = json.loads(r['attributes'] or '{}')
        is_oversize = 'Dimensions' in specs or 'Dimensions' in attrs
        cost = PricingEngine.calculate_dajian_cost(pp, sf, is_oversize)['total_dajian_cost']
        # live eBay price
        try:
            ro = requests.get(f'https://api.ebay.com/sell/inventory/v1/offer?sku={sku}', headers=h, timeout=20)
            offs = ro.json().get('offers', []) if ro.status_code == 200 else []
            live = float(((offs[0].get('pricingSummary') or {}).get('price') or {}).get('value')) if offs else None
        except Exception:
            live = None
        if not live:
            missing += 1; continue
        m = (live - cost) / live
        margins.append(m)
        if m < FLOOR_GROSS:
            new_price = round(cost / (1 - TARGET_GROSS), 2)
            reprice.append({'sku': sku, 'listing_id': r['listing_id'],
                            'live': round(live, 2), 'real_cost': round(cost, 2),
                            'ship': round(sf, 2), 'old_margin': round(m*100, 1),
                            'new_price': new_price})
    db.close()

    import statistics
    print(f"\n=== TRUE margin (incl. freight): checked={len(margins)} missing={missing} ===")
    if margins:
        print(f"  min={min(margins)*100:.1f}%  median={statistics.median(margins)*100:.1f}%  mean={statistics.mean(margins)*100:.1f}%")
    print(f"  below {int(FLOOR_GROSS*100)}% floor: {len(reprice)}")
    json.dump(reprice, open('reprice_targets.json', 'w'), indent=1)
    print(f"  wrote reprice_targets.json ({len(reprice)} items)")
    for x in reprice[:15]:
        print(f"   {x['sku']:14} live=${x['live']} cost=${x['real_cost']} (ship ${x['ship']}) margin={x['old_margin']}% -> new ${x['new_price']}")

if __name__ == '__main__':
    main()
