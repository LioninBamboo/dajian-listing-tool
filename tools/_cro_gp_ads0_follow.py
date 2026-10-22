import os,sys,json,sqlite3,time,requests
from pathlib import Path
ROOT=Path(r"C:\Users\poonx\GrovePop_Listing_Tool")
os.chdir(ROOT); sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/"scripts"))
os.environ["EBAY_ENVIRONMENT"]="PRODUCTION"
from dotenv import load_dotenv; load_dotenv(ROOT/".env", override=True)
from src.services.pricing_engine import PricingEngine
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_ad_service import EbayAdService
import scripts.batch_smart_reprice as bsr

prev=json.loads(Path("reports/cro_traffic_live_gp_20261009.json").read_text(encoding="utf-8"))
skus=[]
for i in prev.get("ads_zero",{}).get("items",[]):
    if i.get("result")=="success" and i.get("sku"):
        skus.append(i["sku"])
# unique
skus=list(dict.fromkeys(skus))
print("followup skus", len(skus), skus[:20])

oauth=EbayOAuthService("PRODUCTION")
adsvc=EbayAdService()
token=oauth.get_valid_token()
headers={"Authorization":f"Bearer {token}","Content-Type":"application/json","Content-Language":"en-US"}
con=sqlite3.connect("ebay_collection.db"); con.row_factory=sqlite3.Row
report={"attempted":0,"success":0,"skipped":0,"failed":0,"items":[]}

def inv(sku):
    r=requests.get(f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}",headers=headers,timeout=30,verify=False)
    for o in (r.json().get("offers") or []):
        if o.get("status")=="PUBLISHED":
            return float(o["pricingSummary"]["price"]["value"]), (o.get("listing") or {}).get("listingId")
    return None, None

for sku in skus:
    item={"sku":sku}
    row=con.execute("SELECT listing_id,cost_breakdown FROM collected_products WHERE sku=?",(sku,)).fetchone()
    if not row: continue
    cost=float(PricingEngine.resolve_landed_cost(json.loads(row["cost_breakdown"] or "{}")) or 0)
    live, lid = inv(sku)
    lid=str(lid or row["listing_id"])
    ad=adsvc.find_ad_for_listing(lid)
    bid=float(ad.get("bidPercentage") or ad.get("bid_percentage") or 0) if ad else 0.0
    floor0=PricingEngine.safe_floor_price(cost,0.10,ad_rate=0.0)
    floor15=PricingEngine.safe_floor_price(cost,0.15,ad_rate=0.0)
    item.update({"lid":lid,"live":live,"bid":bid,"floor0":round(floor0,2),"floor15":round(floor15,2)})
    if live is None or cost<=0:
        item["result"]="skip"; report["skipped"]+=1; report["items"].append(item); continue
    if bid>0:
        # still has ad somehow — delete again
        camp=str(ad.get("campaignId") or ad.get("campaign_id") or "")
        dl=adsvc.delete_ad(camp,lid) if camp else {"success":False}
        item["redelete"]=dl
        time.sleep(0.3)
    target=round(max(floor0, min(live, floor15)),2)
    drop=live-target
    item["target"]=target; item["drop"]=round(drop,2)
    if drop < 1 or drop/live*100 < 5:
        item["result"]="skip"; item["reason"]="room_small"; report["skipped"]+=1; report["items"].append(item); continue
    ok_g, reason = PricingEngine.assert_safe_price(target, cost, safety_margin=0.10, ad_rate=0.0)
    if not ok_g:
        item["result"]="skip"; item["reason"]=f"guard:{reason}"; report["skipped"]+=1; report["items"].append(item); continue
    report["attempted"]+=1
    ok=bsr.update_ebay_price(oauth, sku, target, expected_listing_id=lid)
    post,_=inv(sku)
    item["post"]=post; item["write_ok"]=bool(ok)
    if ok and post is not None and abs(post-target)<=0.05:
        item["result"]="success"; report["success"]+=1
    else:
        item["result"]="fail"; item["reason"]=f"write_ok={ok} post={post}"; report["failed"]+=1
    report["items"].append(item); time.sleep(0.35)

Path("reports/cro_gp_ads0_follow_reprice_20261009.json").write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding="utf-8")
print(json.dumps({k:report[k] for k in ("attempted","success","skipped","failed")},ensure_ascii=False))
print([(i["sku"], i.get("live"), i.get("target"), i.get("post"), i.get("result")) for i in report["items"] if i.get("result")=="success"])
