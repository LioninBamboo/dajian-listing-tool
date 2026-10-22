#!/usr/bin/env python3
"""Backfill the Trading <SKU> (Custom Label) on published Motors listings.

The Trading AddFixedPriceItem builder historically omitted <SKU>, so most auto
listings have a blank Custom Label and no eBay-order -> GigaCloud-SKU linkage.
This revises each published listing to set <SKU> = the stored supplier sku.
--apply to write.
"""
from __future__ import annotations
import os, sys, re, time, argparse, sqlite3
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
import requests
from src.services.ebay_auth import EbayOAuthService

SITEID = os.getenv("EBAY_SITE_ID", "100")
KNOWN_INVENTORY_LISTING_IDS = frozenset({"188683585596"})


def sku_backfill_channel(listing_id, sku=""):
    """Inventory-created leftovers cannot take Trading <SKU> revises."""
    if str(listing_id or "").strip() in KNOWN_INVENTORY_LISTING_IDS:
        return "inventory"
    return "trading"


def live_item_sku(tok, item_id):
    xml = (f'<?xml version="1.0" encoding="utf-8"?><GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           f'<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>'
           f'<ItemID>{item_id}</ItemID></GetItemRequest>')
    h = {'X-EBAY-API-CALL-NAME': 'GetItem', 'X-EBAY-API-SITEID': SITEID,
         'X-EBAY-API-COMPATIBILITY-LEVEL': '1155', 'Content-Type': 'text/xml'}
    r = requests.post('https://api.ebay.com/ws/api.dll', data=xml.encode('utf-8'), headers=h, timeout=30)
    match = re.search(r'<SKU>(.*?)</SKU>', r.text)
    return (match.group(1) or "").strip() if match else ""


def apply_inventory_sku(tok, item_id, sku):
    """Inventory listings already use the inventory-item SKU as Custom Label.

    Trading Revise cannot write <SKU> on these items. Confirm live GetItem
    already carries the supplier SKU; do not invent a second identifier.
    """
    current = live_item_sku(tok, item_id)
    if current == sku:
        return True, "already_set"
    if current:
        return False, f"live SKU is {current}, expected {sku}"
    return False, "inventory listing has empty live SKU; Trading Revise cannot set it"


def revise_sku(tok, item_id, sku, brand="AquaRides"):
    # Motors categories reject a revise unless product identifiers are present;
    # house-brand goods have none, so send the sanctioned placeholders.
    xml = (f'<?xml version="1.0" encoding="utf-8"?><ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           f'<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>'
           f'<Item><ItemID>{item_id}</ItemID><SKU>{sku}</SKU>'
           f'<ProductListingDetails><UPC>Does Not Apply</UPC></ProductListingDetails>'
           f'<ItemSpecifics>'
           f'<NameValueList><Name>Brand</Name><Value>{brand}</Value></NameValueList>'
           f'<NameValueList><Name>Manufacturer Part Number</Name><Value>Does Not Apply</Value></NameValueList>'
           f'</ItemSpecifics></Item></ReviseFixedPriceItemRequest>')
    h = {'X-EBAY-API-CALL-NAME': 'ReviseFixedPriceItem', 'X-EBAY-API-SITEID': SITEID,
         'X-EBAY-API-COMPATIBILITY-LEVEL': '1155', 'Content-Type': 'text/xml'}
    r = requests.post('https://api.ebay.com/ws/api.dll', data=xml.encode('utf-8'), headers=h, timeout=40)
    ack = re.search(r'<Ack>(\w+)</Ack>', r.text)
    if ack and ack.group(1) in ('Success', 'Warning'):
        return True, ''
    m = re.search(r'<LongMessage>(.*?)</LongMessage>', r.text)
    return False, (m.group(1)[:120] if m else r.text[:120])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sku", action="append", default=[], help="limit to SKU; repeatable")
    args = ap.parse_args()
    db = sqlite3.connect('ebay_collection.db')
    rows = db.execute("SELECT sku, listing_id FROM collected_products "
                      "WHERE status='PUBLISHED' AND listing_id!='' AND sku!=''").fetchall()
    db.close()
    wanted = {str(s).strip() for s in args.sku if str(s).strip()}
    if wanted:
        rows = [r for r in rows if r[0] in wanted]
    print(f"{'APPLY' if args.apply else 'DRY'} — {len(rows)} listings")
    tok = EbayOAuthService(environment='PRODUCTION').get_valid_token()
    ok = fail = 0
    for i, (sku, iid) in enumerate(rows, 1):
        channel = sku_backfill_channel(iid, sku)
        if not args.apply:
            print(f"  [{i}] {iid} -> SKU {sku} ({channel})")
            ok += 1
            continue
        if channel == "inventory":
            good, err = apply_inventory_sku(tok, iid, sku)
        else:
            good, err = revise_sku(tok, iid, sku)
        if good:
            ok += 1
            print(f"  [{i}] {iid} SKU={sku} OK {err}".rstrip())
        else:
            fail += 1
            print(f"  [{i}] {iid} FAIL: {err}")
        time.sleep(0.4)
    print(f"\ndone: {ok} ok, {fail} fail")


if __name__ == '__main__':
    main()
