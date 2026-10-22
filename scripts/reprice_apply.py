#!/usr/bin/env python3
"""Apply targeted reprice from reprice_fix.json to live eBay offers.

Each row: {sku, listing_id, live, real_cost, ship, old_margin, new_price}.
For each SKU: GET the offer, bump pricingSummary.price.value to new_price, PUT it
back preserving every other field. Also writes the real freight back to the DB
row's shipping column so future cost math is correct. --apply to write.
"""
from __future__ import annotations
import os, sys, json, time, argparse, sqlite3
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
import requests
from src.services.ebay_auth import EbayOAuthService

BASE = "https://api.ebay.com"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="reprice_fix.json")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    fixes = json.load(open(args.file))
    print(f"{'APPLY' if args.apply else 'DRY'} — {len(fixes)} offers to reprice")

    oauth = EbayOAuthService(environment='PRODUCTION')
    tok = oauth.get_valid_token()
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
         "Content-Language": "en-US", "Accept": "application/json"}
    db = sqlite3.connect('ebay_collection.db')

    ok = 0; fail = 0
    for i, f in enumerate(fixes, 1):
        sku = f['sku']; new_price = f['new_price']
        # write real freight back to DB regardless
        if args.apply and f.get('ship') is not None:
            db.execute("UPDATE collected_products SET shipping=? WHERE sku=?", (f['ship'], sku))
        # find the offer for this sku
        ro = requests.get(f"{BASE}/sell/inventory/v1/offer?sku={sku}", headers=H, timeout=30)
        if ro.status_code != 200 or not ro.json().get('offers'):
            print(f"  [{i}] {sku}: no offer ({ro.status_code})"); fail += 1; continue
        offer = ro.json()['offers'][0]
        oid = offer['offerId']
        cur = ((offer.get('pricingSummary') or {}).get('price') or {}).get('value')
        if not args.apply:
            print(f"  [{i}] {sku}: ${cur} -> ${new_price} (offer {oid})")
            ok += 1; continue
        # modify only the price, PUT the full offer body back (proven pattern)
        offer.setdefault('pricingSummary', {}).setdefault('price', {})
        offer['pricingSummary']['price']['value'] = str(round(new_price, 2))
        offer['pricingSummary']['price']['currency'] = 'USD'
        pu = requests.put(f"{BASE}/sell/inventory/v1/offer/{oid}", headers=H,
                          data=json.dumps(offer), timeout=40)
        if pu.status_code in (200, 204):
            print(f"  [{i}] {sku}: ${cur} -> ${new_price} OK")
            ok += 1
        else:
            print(f"  [{i}] {sku}: FAIL {pu.status_code} {pu.text[:120]}")
            fail += 1
        time.sleep(0.3)
    if args.apply:
        db.commit()
    db.close()
    print(f"\ndone: {ok} repriced, {fail} failed")


if __name__ == '__main__':
    main()
