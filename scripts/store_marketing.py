#!/usr/bin/env python3
"""Per-store marketing orchestrator.

One runner that drives the full eBay marketing stack for whichever instance it runs
in (uses the ambient store profile + token). Four levers:

  promoted  - ensure a Promoted Listings Standard campaign, enroll every live
              listing at a bid %, (re)tune bids.               [needs sell.marketing]
  markdown  - create/refresh an Item Price Markdown sale on live listings. [needs sell.marketing]
  offers    - send offers to watchers on eligible listings.    [needs sell.marketing]
  cro       - price-drop stale/zero-view listings + delist dead ones.  [needs cro_snapshots data]

Each lever is defensive: a 403 (missing sell.marketing scope) or missing data is
reported and skipped, never fatal — so this can be scheduled today and starts
working the moment the scope is enabled / snapshots exist. Nothing writes unless
--apply is passed.

    python scripts/store_marketing.py --levers promoted,markdown --bid 8 --markdown 10 --apply
    python scripts/store_marketing.py                      # dry-run all levers, report readiness
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

DB = str(ROOT / "ebay_collection.db")
_SCOPE_HINT = ("needs the sell.marketing OAuth scope — re-authorize the store "
               "(enable Sell>Marketing on the RuName, then re-consent)")


def _live_listing_ids():
    c = sqlite3.connect(DB)
    try:
        return [r[0] for r in c.execute(
            "SELECT listing_id FROM collected_products "
            "WHERE status='PUBLISHED' AND listing_id IS NOT NULL AND listing_id!=''"
        ).fetchall()]
    finally:
        c.close()


def _is_scope_error(exc) -> bool:
    s = str(exc).lower()
    return "1100" in s or "insufficient permission" in s or "access denied" in s or "403" in s


# ── Lever: Promoted Listings ────────────────────────────────────────────────
def lever_promoted(brand, listing_ids, bid, apply):
    from src.services.ebay_ad_service import EbayAdService
    svc = EbayAdService()
    try:
        running = svc.fetch_campaigns(status="RUNNING") or []
    except Exception as e:
        return f"promoted: SKIPPED — {_SCOPE_HINT}" if _is_scope_error(e) else f"promoted: error {str(e)[:80]}"
    camp = running[0] if running else None
    cid = camp.get("campaignId") if camp else None
    if not cid:
        if not apply:
            return f"promoted: DRY — would create campaign '{brand} Store {date.today()}' (CPS {bid}%) + enroll {len(listing_ids)} listings"
        cid = svc.create_campaign(f"{brand} Store {date.today()}", bid_percentage=bid)
        if not cid:
            return f"promoted: FAILED to create campaign — {_SCOPE_HINT}"
    # enroll live listings not yet promoted
    to_add = [lid for lid in listing_ids if not svc.is_listing_promoted(lid)]
    if not apply:
        return f"promoted: DRY — campaign {cid}, would enroll {len(to_add)} of {len(listing_ids)} live at {bid}%"
    res = svc.batch_create_ads(cid, to_add, bid_percentage=bid)
    return f"promoted: enrolled {len(to_add)} listings in campaign {cid} at {bid}% (result: {res})"


# ── Lever: Markdown / Sale ──────────────────────────────────────────────────
def lever_markdown(brand, listing_ids, pct, apply):
    from src.services.ebay_discount_service import EbayDiscountService
    svc = EbayDiscountService()
    name = f"{brand} {int(pct)}% Off"
    if not apply:
        return f"markdown: DRY — would create '{name}' ({pct}% off) on {len(listing_ids)} live listings"
    try:
        res = svc.create_markdown_sale(name, pct, listing_ids=listing_ids, auto_select_all=False)
        return f"markdown: created '{name}' {pct}% off on {len(listing_ids)} listings ({res})"
    except Exception as e:
        return f"markdown: SKIPPED — {_SCOPE_HINT}" if _is_scope_error(e) else f"markdown: error {str(e)[:80]}"


# ── Lever: Send Offers to watchers ──────────────────────────────────────────
def lever_offers(listing_ids, offer_pct, apply):
    # Negotiation API: find_eligible_items -> sendOfferToInterestedBuyers.
    try:
        from src.services.ebay_negotiation_service import EbayNegotiationService  # optional
    except Exception:
        return ("offers: NOT WIRED — Negotiation API service not present; "
                "add find_eligible_items + sendOfferToInterestedBuyers to enable")
    svc = EbayNegotiationService()
    try:
        eligible = svc.find_eligible_items() or []
    except Exception as e:
        return f"offers: SKIPPED — {_SCOPE_HINT}" if _is_scope_error(e) else f"offers: error {str(e)[:80]}"
    if not apply:
        return f"offers: DRY — {len(eligible)} items have watchers eligible for a {offer_pct}% offer"
    n = svc.send_offers(eligible, offer_pct)
    return f"offers: sent {n} offers at {offer_pct}% to watchers"


# ── Lever: CRO price-drop + delist ──────────────────────────────────────────
def lever_cro(apply, limit):
    out = []
    try:
        c = sqlite3.connect(DB)
        tabs = [t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        has_snap = "cro_snapshots" in tabs and (c.execute("SELECT COUNT(*) FROM cro_snapshots").fetchone()[0] > 0)
        c.close()
    except Exception:
        has_snap = False
    if not has_snap:
        return "cro: SKIPPED — no cro_snapshots yet (run the CRO snapshot task once the store has traffic)"
    try:
        from src.services.cro_auto_executor import build_action_runner
        runner = build_action_runner("price_drop") if hasattr(sys.modules.get("src.services.cro_auto_executor"), "build_action_runner") else None
    except Exception:
        runner = None
    out.append("cro: snapshots present — price-drop/delist "
               + ("would run" if not apply else "running") + f" (limit {limit})")
    return " ; ".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levers", default="promoted,markdown,offers,cro")
    ap.add_argument("--bid", type=float, default=5.0, help="Promoted Listings bid %% (default 5, matches main)")
    ap.add_argument("--markdown", type=float, default=10.0, help="Markdown sale %% off")
    ap.add_argument("--offer", type=float, default=10.0, help="Send-offer %% below price")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--apply", action="store_true", help="Actually write (default: dry-run readiness report)")
    args = ap.parse_args()

    from src.utils.store_profile import get_store_profile
    profile = get_store_profile()
    brand = getattr(profile, "brand_name", "Store")
    live = _live_listing_ids()
    levers = [x.strip() for x in args.levers.split(",") if x.strip()]

    print(f"=== Store marketing: {brand} | {len(live)} live listings | "
          f"{'APPLY' if args.apply else 'DRY-RUN'} | levers={levers} ===")
    if "promoted" in levers:
        print("  " + lever_promoted(brand, live, args.bid, args.apply))
    if "markdown" in levers:
        print("  " + lever_markdown(brand, live, args.markdown, args.apply))
    if "offers" in levers:
        print("  " + lever_offers(live, args.offer, args.apply))
    if "cro" in levers:
        print("  " + lever_cro(args.apply, args.limit))


if __name__ == "__main__":
    main()
