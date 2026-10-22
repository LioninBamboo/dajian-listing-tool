#!/usr/bin/env python3
"""Re-push all published Motors descriptions through the ascii_safe Trading path.

eBay mangled multi-byte chars (× é) in the live CDATA to U+FFFD. The description
builder produces clean text; motors_trading.ascii_safe (now wired into
build_revise_fixed_price_item_xml) makes it round-trip cleanly. This rebuilds
each listing's description from source and revises it. --apply to write.
"""
from __future__ import annotations
import os, sys, re, json, time, argparse, sqlite3
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
import requests
from src.services.ebay_auth import EbayOAuthService
from src.services.semantic_rewrite import build_description_from_source
from src.services.motors_trading import build_revise_fixed_price_item_xml

SITEID = os.getenv("EBAY_SITE_ID", "100")
API = "https://api.ebay.com/ws/api.dll"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = sqlite3.connect('ebay_collection.db'); db.row_factory = sqlite3.Row
    rows = db.execute("SELECT sku, listing_id, title, description, attributes, specs, optimization "
                      "FROM collected_products WHERE status='PUBLISHED' AND listing_id!=''").fetchall()
    db.close()
    tok = EbayOAuthService(environment='PRODUCTION').get_valid_token()
    # build_revise_fixed_price_item_xml carries no RequesterCredentials, so auth
    # must ride in the IAF-TOKEN header (same as real_ebay_client Trading calls).
    hdr = {"X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem", "X-EBAY-API-SITEID": SITEID,
           "X-EBAY-API-COMPATIBILITY-LEVEL": "1155", "X-EBAY-API-IAF-TOKEN": tok,
           "Content-Type": "text/xml"}
    ok = fail = skip = 0
    print(f"{'APPLY' if args.apply else 'DRY'} — {len(rows)} listings")
    for i, r in enumerate(rows, 1):
        attrs = json.loads(r["attributes"] or "{}"); specs = json.loads(r["specs"] or "{}")
        asp = json.loads(r["optimization"] or "{}").get("aspects", {})
        desc = build_description_from_source(
            title=r["title"], source_description=r["description"] or "",
            attrs=attrs, specs=specs, aspects=asp)
        if not desc or len(desc) < 800:
            skip += 1
            print(f"  [{i}] {r['sku']} SKIP: rebuilt desc too short ({len(desc or '')})")
            continue
        if not args.apply:
            print(f"  [{i}] {r['sku']} {r['listing_id']}: would repush ({len(desc)} chars)")
            ok += 1
            continue
        xml = build_revise_fixed_price_item_xml(item_id=str(r["listing_id"]), description=desc)
        try:
            resp = requests.post(API, data=xml.encode("utf-8"), headers=hdr, timeout=45)
            ack = (re.search(r"<Ack>(\w+)</Ack>", resp.text) or [None, "?"])[1]
        except Exception as e:  # noqa: BLE001
            ack = f"EXC:{e}"
        if ack in ("Success", "Warning"):
            ok += 1
            print(f"  [{i}] {r['sku']} {r['listing_id']} OK")
        else:
            fail += 1
            m = re.search(r"<LongMessage>(.*?)</LongMessage>", getattr(resp, 'text', '') or '')
            print(f"  [{i}] {r['sku']} FAIL: {(m.group(1)[:110] if m else ack)}")
        time.sleep(0.4)
    print(f"\ndone: {ok} ok, {fail} fail, {skip} skip")


if __name__ == "__main__":
    main()
