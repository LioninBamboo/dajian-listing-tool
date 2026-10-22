#!/usr/bin/env python3
"""Backfill real GigaCloud dropship freight into collected_products.shipping.

The collector historically stored shipping=0, so any pricing task that reads
product.shipping computed an understated landed cost. This fetches the live
shippingFee for every row (paced, chunked) and writes it back, making the DB
safe for scheduled smart-reprice. --apply to write.
"""
from __future__ import annotations
import os, sys, json, time, argparse, sqlite3
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
from src.clients.dajian_client import DaJianClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--status", default=None, help="only rows with this status (default: all)")
    args = ap.parse_args()
    db = sqlite3.connect('ebay_collection.db')
    q = "SELECT sku, shipping FROM collected_products WHERE sku IS NOT NULL AND sku!=''"
    if args.status:
        q += f" AND status='{args.status}'"
    rows = db.execute(q).fetchall()
    skus = [r[0] for r in rows]
    cur_ship = {r[0]: (r[1] or 0) for r in rows}
    print(f"rows={len(skus)} {'APPLY' if args.apply else 'DRY'}")
    dj = DaJianClient(os.getenv('DAJIAN_API_KEY'), os.getenv('DAJIAN_API_SECRET'))
    updated = 0; zero_before = sum(1 for v in cur_ship.values() if not v)
    for i in range(0, len(skus), 30):
        chunk = skus[i:i+30]
        for a in range(5):
            try:
                pr = dj.get_product_prices(chunk) or []; break
            except Exception:
                pr = []; time.sleep(8*(a+1))
        for p in pr:
            if not (isinstance(p, dict) and p.get('sku')):
                continue
            sf = p.get('shippingFee')
            if sf is None:
                sf = (p.get('shippingFeeRange') or {}).get('maxAmount')
            if sf is None:
                continue
            sf = float(sf)
            if abs(sf - float(cur_ship.get(p['sku'], 0))) > 0.01:
                if args.apply:
                    db.execute("UPDATE collected_products SET shipping=? WHERE sku=?", (sf, p['sku']))
                updated += 1
        time.sleep(5)
        print(f"  {min(i+30,len(skus))}/{len(skus)} processed", flush=True)
    if args.apply:
        db.commit()
    db.close()
    print(f"done: shipping had {zero_before} zero rows; {updated} rows {'updated' if args.apply else 'would update'}")


if __name__ == '__main__':
    main()
