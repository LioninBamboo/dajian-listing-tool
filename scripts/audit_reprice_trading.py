#!/usr/bin/env python3
"""True-margin audit + reprice for Trading-channel listings (AquaRides).

Same freight bug as garden: collection ignored GigaCloud shippingFee, so cost was
understated and prices set too low. This fetches real shippingFee, recomputes
landed cost, reads the LIVE Trading price (GetItem), and for any listing whose
true margin is below FIX_BELOW, revises the price up to TARGET gross via
ReviseFixedPriceItem. --apply to write; also writes real freight back to DB.
"""
from __future__ import annotations
import os, sys, json, time, re, argparse, sqlite3
from xml.sax.saxutils import escape
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
import requests
from src.clients.dajian_client import DaJianClient
from src.services.pricing_engine import PricingEngine
from src.services.ebay_auth import EbayOAuthService

FIX_BELOW = 15.0
TARGET = 0.30
SITEID = os.getenv("EBAY_SITE_ID", "100")


def build_revise_price_xml(item_id, new_price, *, token, brand="AquaRides"):
    """Trading Revise payload with Motors-required UPC / Brand / MPN placeholders."""
    from src.services.motors_trading import build_revise_fixed_price_item_xml

    item_xml = build_revise_fixed_price_item_xml(
        item_id=item_id, start_price=new_price
    )
    # Motors categories often require product identifiers on revise even for
    # price-only updates. Inject UPC + Brand + MPN placeholders into <Item>.
    brand_text = escape(str(brand or "AquaRides"))
    identifiers = (
        "<ProductListingDetails><UPC>Does Not Apply</UPC></ProductListingDetails>"
        "<ItemSpecifics>"
        f"<NameValueList><Name>Brand</Name><Value>{brand_text}</Value></NameValueList>"
        "<NameValueList><Name>Manufacturer Part Number</Name>"
        "<Value>Does Not Apply</Value></NameValueList>"
        "</ItemSpecifics>"
    )
    item_xml = item_xml.replace("</Item>", f"{identifiers}</Item>", 1)
    return item_xml.replace(
        "<ReviseFixedPriceItemRequest xmlns=\"urn:ebay:apis:eBLBaseComponents\">",
        "<ReviseFixedPriceItemRequest xmlns=\"urn:ebay:apis:eBLBaseComponents\">"
        f"<RequesterCredentials><eBayAuthToken>{escape(str(token))}</eBayAuthToken></RequesterCredentials>",
        1,
    )


def getitem_price(tok, item_id):
    xml = (f'<?xml version="1.0" encoding="utf-8"?><GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           f'<RequesterCredentials><eBayAuthToken>{tok}</eBayAuthToken></RequesterCredentials>'
           f'<ItemID>{item_id}</ItemID></GetItemRequest>')
    h = {'X-EBAY-API-CALL-NAME': 'GetItem', 'X-EBAY-API-SITEID': SITEID,
         'X-EBAY-API-COMPATIBILITY-LEVEL': '1155', 'Content-Type': 'text/xml'}
    r = requests.post('https://api.ebay.com/ws/api.dll', data=xml.encode('utf-8'), headers=h, timeout=30)
    m = re.search(r'<CurrentPrice[^>]*>([0-9.]+)</CurrentPrice>', r.text) or \
        re.search(r'<StartPrice[^>]*>([0-9.]+)</StartPrice>', r.text)
    return float(m.group(1)) if m else None


def revise_price(tok, item_id, new_price, brand="AquaRides"):
    # Motors/tools categories reject a revise without product identifiers; house-brand
    # goods have none, so send UPC/Brand/MPN placeholders together.
    xml = build_revise_price_xml(item_id, new_price, token=tok, brand=brand)
    h = {'X-EBAY-API-CALL-NAME': 'ReviseFixedPriceItem', 'X-EBAY-API-SITEID': SITEID,
         'X-EBAY-API-COMPATIBILITY-LEVEL': '1155', 'Content-Type': 'text/xml'}
    r = requests.post('https://api.ebay.com/ws/api.dll', data=xml.encode('utf-8'), headers=h, timeout=40)
    ok = '<Ack>Success</Ack>' in r.text or '<Ack>Warning</Ack>' in r.text
    if not ok:
        m = re.search(r'<LongMessage>(.*?)</LongMessage>', r.text)
        return False, (m.group(1)[:120] if m else r.text[:120])
    return True, ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sku", action="append", default=[], help="limit to SKU; repeatable")
    args = ap.parse_args()
    db = sqlite3.connect('ebay_collection.db'); db.row_factory = sqlite3.Row
    rows = db.execute("SELECT sku,listing_id,specs,attributes FROM collected_products "
                      "WHERE status='PUBLISHED' AND listing_id!=''").fetchall()
    wanted = {str(s).strip() for s in args.sku if str(s).strip()}
    if wanted:
        rows = [r for r in rows if r["sku"] in wanted]
    dj = DaJianClient(os.getenv('DAJIAN_API_KEY'), os.getenv('DAJIAN_API_SECRET'))
    skus = [r['sku'] for r in rows]
    ps = {}
    for i in range(0, len(skus), 30):
        chunk = skus[i:i+30]
        for a in range(5):
            try:
                pr = dj.get_product_prices(chunk) or []; break
            except Exception:
                pr = []; time.sleep(8*(a+1))
        for p in pr:
            if isinstance(p, dict) and p.get('sku'):
                ps[p['sku']] = (float(p.get('price') or 0), float(p.get('shippingFee') or 0))
        time.sleep(5)

    tok = EbayOAuthService(environment='PRODUCTION').get_valid_token()
    fixed = 0; failed = 0; checked = 0; below = 0
    for r in rows:
        pp, sf = ps.get(r['sku'], (0, 0))
        if not pp:
            continue
        if args.apply and sf:
            db.execute("UPDATE collected_products SET shipping=? WHERE sku=?", (sf, r['sku']))
        specs = json.loads(r['specs'] or '{}'); attrs = json.loads(r['attributes'] or '{}')
        is_over = 'Dimensions' in specs or 'Dimensions' in attrs
        cost = PricingEngine.calculate_dajian_cost(pp, sf, is_over)['total_dajian_cost']
        live = getitem_price(tok, r['listing_id'])
        if not live:
            continue
        checked += 1
        m = (live - cost) / live * 100
        if m >= FIX_BELOW:
            continue
        below += 1
        new_price = round(cost / (1 - TARGET), 2)
        if not args.apply:
            print(f"  {r['sku']:14} live=${live:.2f} cost=${cost:.2f} (ship ${sf}) margin={m:.1f}% -> ${new_price}")
            continue
        ok, err = revise_price(tok, r['listing_id'], new_price)
        if ok:
            verified = getitem_price(tok, r['listing_id'])
            if verified is None or abs(verified - new_price) > 0.05:
                failed += 1
                print(f"  {r['sku']:14} VERIFY FAIL live={verified} expected={new_price}")
            else:
                fixed += 1
                print(f"  {r['sku']:14} ${live:.2f}->${verified} OK")
        else:
            failed += 1
            print(f"  {r['sku']:14} FAIL: {err}")
        time.sleep(0.4)
    if args.apply:
        db.commit()
    db.close()
    print(f"\n=== AquaRides: checked={checked} below{int(FIX_BELOW)}%={below} "
          f"{'fixed='+str(fixed)+' failed='+str(failed) if args.apply else '(dry-run)'} ===")


if __name__ == '__main__':
    main()
