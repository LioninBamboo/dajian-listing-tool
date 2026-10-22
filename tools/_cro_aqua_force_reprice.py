import os, sys, json, sqlite3, time, requests
from pathlib import Path
ROOT = Path(r"C:\Users\poonx\AutoParts_Listing_Tool")
os.chdir(ROOT); sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/"scripts"))
os.environ["EBAY_ENVIRONMENT"] = "PRODUCTION"
from dotenv import load_dotenv; load_dotenv(ROOT/".env", override=True)
from src.services.pricing_engine import PricingEngine
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_ad_service import EbayAdService
from src.services.ebay_performance import EbayPerformanceService

oauth = EbayOAuthService("PRODUCTION")
token = oauth.get_valid_token()
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Content-Language": "en-US"}
adsvc = EbayAdService()
perf = EbayPerformanceService()
traffic = perf.fetch_traffic_data(days=30)
con = sqlite3.connect("ebay_collection.db"); con.row_factory = sqlite3.Row
lid_map = {str(r["listing_id"]): dict(r) for r in con.execute("SELECT sku,listing_id,cost_breakdown,title,status FROM collected_products WHERE status='PUBLISHED'") if r["listing_id"]}

def get_offer(sku):
    r = requests.get(f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}", headers=headers, timeout=30, verify=False)
    for o in (r.json().get("offers") or []):
        if o.get("status") == "PUBLISHED":
            return o
    return None

def put_price(offer, new_price):
    oid = offer["offerId"]
    offer = json.loads(json.dumps(offer))  # copy
    offer["pricingSummary"]["price"]["value"] = str(round(new_price, 2))
    put = requests.put(f"https://api.ebay.com/sell/inventory/v1/offer/{oid}", headers=headers, json=offer, timeout=60, verify=False)
    if put.status_code not in (200, 204):
        return False, f"PUT {put.status_code} {put.text[:200]}"
    # re-get verify
    time.sleep(0.4)
    o2 = get_offer(offer["sku"])
    post = float(o2["pricingSummary"]["price"]["value"]) if o2 else None
    return (post is not None and abs(post - new_price) <= 0.05), f"post={post}"

cands = []
for lid, m in traffic.items():
    if (m.get("transactions") or 0) > 0: continue
    imp = int(m.get("impressions") or 0)
    if imp < 80: continue
    row = lid_map.get(str(lid))
    if not row: continue
    sku = row["sku"]
    if str(sku).startswith("N735"): continue
    cands.append((imp, str(lid), m, row))
cands.sort(reverse=True)

report = {"attempted":0,"success":0,"skipped":0,"failed":0,"items":[]}
for imp, lid, m, row in cands[:60]:
    sku = row["sku"]
    item = {"sku": sku, "lid": lid, "imp": imp}
    try:
        cost = float(PricingEngine.resolve_landed_cost(json.loads(row["cost_breakdown"] or "{}")) or 0)
        offer = get_offer(sku)
        if not offer or cost <= 0:
            item["result"] = "skip"; item["reason"] = "no_offer_or_cost"; report["skipped"] += 1
            report["items"].append(item); continue
        live = float(offer["pricingSummary"]["price"]["value"])
        ad = adsvc.find_ad_for_listing(lid)
        bid = float(ad.get("bidPercentage") or ad.get("bid_percentage") or 0) if ad else 0.0
        ad_rate = (bid/100.0) if bid > 0 else 0.05
        floor10 = PricingEngine.safe_floor_price(cost, 0.10, ad_rate=ad_rate if bid>0 else 0.05)
        floor15 = PricingEngine.safe_floor_price(cost, 0.15, ad_rate=ad_rate if bid>0 else 0.05)
        if live <= floor15 + 0.009:
            item["result"] = "skip"; item["reason"] = "already_safe15"; item["live"]=live; item["floor15"]=round(floor15,2)
            report["skipped"] += 1; report["items"].append(item); continue
        target = round(floor15, 2)
        drop = live - target
        room = drop/live*100
        item.update({"live":live,"cost":round(cost,2),"bid":bid,"floor10":round(floor10,2),"floor15":round(floor15,2),"target":target,"drop":round(drop,2),"room":round(room,1)})
        if drop < 1 or room < 5:
            item["result"]="skip"; item["reason"]="room_small"; report["skipped"]+=1; report["items"].append(item); continue
        ok_g, reason = PricingEngine.assert_safe_price(target, cost, safety_margin=0.10, ad_rate=ad_rate if bid>0 else 0.05)
        if not ok_g:
            item["result"]="skip"; item["reason"]=f"guard:{reason}"; report["skipped"]+=1; report["items"].append(item); continue
        report["attempted"] += 1
        ok, msg = put_price(offer, target)
        item["verify"] = msg; item["write_ok"] = ok
        if ok:
            item["result"]="success"; report["success"] += 1
        else:
            item["result"]="fail"; item["reason"]=msg; report["failed"] += 1
    except Exception as e:
        item["result"]="fail"; item["reason"]=repr(e); report["failed"] += 1; report["attempted"] += 1
    report["items"].append(item)
    time.sleep(0.35)

Path("reports/cro_aqua_reprice_force_20261009.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k:report[k] for k in ("attempted","success","skipped","failed")}, ensure_ascii=False))
oks=[i for i in report["items"] if i.get("result")=="success"]
print("examples", [(i["sku"], i.get("live"), i.get("target"), i.get("imp")) for i in oks[:15]])
