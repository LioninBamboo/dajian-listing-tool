import os, json, sqlite3, requests, sys
from pathlib import Path
ROOT = Path(r"C:\Users\poonx\GrovePop_Listing_Tool")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["EBAY_ENVIRONMENT"] = "PRODUCTION"
from dotenv import load_dotenv
load_dotenv(ROOT/".env", override=True)
from src.services.pricing_engine import PricingEngine
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_ad_service import EbayAdService
from src.services.ebay_performance import EbayPerformanceService

oauth = EbayOAuthService("PRODUCTION")
token = oauth.get_valid_token()
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
adsvc = EbayAdService()
perf = EbayPerformanceService()
traffic = perf.fetch_traffic_data(days=30)
print("GP traffic", perf.last_traffic_fetch_meta, "n", len(traffic))
con = sqlite3.connect("ebay_collection.db")
con.row_factory = sqlite3.Row
# map listing_id -> product
lid_map = {}
for r in con.execute("SELECT sku, listing_id, price, title, cost_breakdown, status FROM collected_products WHERE status='PUBLISHED'"):
    if r["listing_id"]:
        lid_map[str(r["listing_id"])] = r

zero = []
for lid, m in traffic.items():
    if (m.get("transactions") or 0) == 0 and (m.get("impressions") or 0) >= 100:
        zero.append((m.get("impressions") or 0, lid, m))
zero.sort(reverse=True)
print("zero>=100imp", len(zero))
rows=[]
for imp, lid, m in zero[:50]:
    r = lid_map.get(str(lid))
    if not r:
        rows.append({"lid": lid, "imp": imp, "missing_db": True}); continue
    sku=r["sku"]
    cost=float(PricingEngine.resolve_landed_cost(json.loads(r["cost_breakdown"] or "{}")) or 0)
    ir=requests.get(f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}", headers=headers, timeout=30, verify=False)
    live=None; offer_id=None
    for o in (ir.json().get("offers") or []):
        if o.get("status")=="PUBLISHED":
            live=float(o["pricingSummary"]["price"]["value"]); offer_id=o.get("offerId"); break
    ad=adsvc.find_ad_for_listing(str(lid)) if lid else None
    bid=float(ad.get("bidPercentage") or ad.get("bid_percentage") or 0) if ad else 0.0
    ad_rate=(bid/100.0) if bid else 0.05
    floor10=PricingEngine.safe_floor_price(cost, 0.10, ad_rate=ad_rate if bid else 0.05)
    floor15=PricingEngine.safe_floor_price(cost, 0.15, ad_rate=ad_rate if bid else 0.05)
    room=((live or 0)-floor10)/live*100 if live else 0
    rows.append({"sku":sku,"lid":lid,"live":live,"cost":round(cost,2),"bid":bid,"floor10":round(floor10,2),"floor15":round(floor15,2),"room_pct":round(room,1),"imp":imp,"views":m.get("views") or 0,"ctr":m.get("ctr") or 0,"title_len":len(r["title"] or ""),"title":(r["title"] or "")[:70]})
can=[x for x in rows if x.get("live") and x["room_pct"]>=5 and (x["live"]-x["floor10"])>=1]
noad=[x for x in rows if x.get("bid",0)<=0 and not x.get("missing_db")]
print("can_cut", len(can), "no_ad", len(noad), "checked", len(rows))
for x in can[:15]: print("CUT", x)
for x in sorted(noad, key=lambda z:-z.get("imp",0))[:15]: print("NOAD", x["sku"], x["imp"], x.get("live"), x.get("floor10"), "room", x.get("room_pct"), x.get("title"))
Path("reports").mkdir(exist_ok=True)
Path("reports/cro_gp_opportunity_20261009.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
print("wrote", len(rows))
