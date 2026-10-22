import os, sys, json, sqlite3, time
from pathlib import Path
import xml.etree.ElementTree as ET
ROOT = Path(r"C:\Users\poonx\AutoParts_Listing_Tool")
os.chdir(ROOT); sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/"scripts"))
os.environ["EBAY_ENVIRONMENT"] = "PRODUCTION"
from dotenv import load_dotenv; load_dotenv(ROOT/".env", override=True)
from src.services.pricing_engine import PricingEngine
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_ad_service import EbayAdService
from src.services.ebay_performance import EbayPerformanceService
import scripts.batch_smart_reprice as bsr
import cro_title_rewrite as ctr

NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
oauth = EbayOAuthService("PRODUCTION")
adsvc = EbayAdService()
perf = EbayPerformanceService()
traffic = perf.fetch_traffic_data(days=30)
tclient = ctr._get_trading_client()
con = sqlite3.connect("ebay_collection.db"); con.row_factory = sqlite3.Row
lid_map = {str(r["listing_id"]): dict(r) for r in con.execute("SELECT sku,listing_id,cost_breakdown,title,status FROM collected_products WHERE status='PUBLISHED'") if r["listing_id"]}

def live_price(lid):
    resp = tclient.call("GetItem", f"<ItemID>{lid}</ItemID><DetailLevel>ReturnAll</DetailLevel>")
    root = ET.fromstring(resp)
    p = root.find(".//e:Item/e:StartPrice", NS)
    return float(p.text) if p is not None and p.text else None

cands = []
for lid, m in traffic.items():
    if (m.get("transactions") or 0) > 0: continue
    imp = int(m.get("impressions") or 0)
    if imp < 80: continue
    row = lid_map.get(str(lid))
    if not row: continue
    if str(row["sku"]).startswith("N735"): continue
    cands.append((imp, str(lid), m, row))
cands.sort(reverse=True)

report = {"attempted":0,"success":0,"skipped":0,"failed":0,"items":[]}
for imp, lid, m, row in cands[:40]:
    sku = row["sku"]
    item = {"sku": sku, "lid": lid, "imp": imp}
    try:
        cost = float(PricingEngine.resolve_landed_cost(json.loads(row["cost_breakdown"] or "{}")) or 0)
        live = live_price(lid)
        if live is None or cost <= 0:
            item["result"]="skip"; item["reason"]="no_live_or_cost"; report["skipped"]+=1; report["items"].append(item); continue
        ad = adsvc.find_ad_for_listing(lid)
        bid = float(ad.get("bidPercentage") or ad.get("bid_percentage") or 0) if ad else 0.0
        ad_rate = (bid/100.0) if bid > 0 else 0.05
        floor10 = PricingEngine.safe_floor_price(cost, 0.10, ad_rate=ad_rate if bid>0 else 0.05)
        floor15 = PricingEngine.safe_floor_price(cost, 0.15, ad_rate=ad_rate if bid>0 else 0.05)
        item.update({"live":live,"cost":round(cost,2),"bid":bid,"floor10":round(floor10,2),"floor15":round(floor15,2)})
        if live <= floor15 + 0.009:
            item["result"]="skip"; item["reason"]="already_safe15"; report["skipped"]+=1; report["items"].append(item); continue
        target = round(floor15, 2)
        drop = live - target; room = drop/live*100
        item.update({"target":target,"drop":round(drop,2),"room":round(room,1)})
        if drop < 1 or room < 5:
            item["result"]="skip"; item["reason"]="room_small"; report["skipped"]+=1; report["items"].append(item); continue
        ok_g, reason = PricingEngine.assert_safe_price(target, cost, safety_margin=0.10, ad_rate=ad_rate if bid>0 else 0.05)
        if not ok_g:
            item["result"]="skip"; item["reason"]=f"guard:{reason}"; report["skipped"]+=1; report["items"].append(item); continue
        report["attempted"] += 1
        ok = bsr.update_ebay_price(oauth, sku, target, expected_listing_id=lid)
        post = live_price(lid)
        item["post"]=post; item["write_ok"]=bool(ok)
        if ok and post is not None and abs(post-target) <= 0.05:
            item["result"]="success"; report["success"] += 1
        elif ok and post is not None and post < live - 0.5:
            item["result"]="success_partial"; item["note"]="moved but not exact"; report["success"] += 1
        else:
            item["result"]="fail"; item["reason"]=f"write_ok={ok} post={post}"; report["failed"] += 1
    except Exception as e:
        item["result"]="fail"; item["reason"]=repr(e); report["failed"]+=1; report["attempted"]+=1
    report["items"].append(item)
    Path("reports/cro_aqua_trading_reprice_20261009.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.45)

print(json.dumps({k:report[k] for k in ("attempted","success","skipped","failed")}, ensure_ascii=False))
oks=[i for i in report["items"] if "success" in str(i.get("result"))]
print("ok examples", [(i["sku"], i.get("live"), i.get("target"), i.get("post"), i.get("imp")) for i in oks[:20]])
fails=[i for i in report["items"] if i.get("result")=="fail"][:8]
print("fails", fails)
