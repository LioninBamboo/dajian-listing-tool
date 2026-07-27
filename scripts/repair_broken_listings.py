#!/usr/bin/env python3
"""Repair listings whose LIVE content is broken: raw-source (untemplated)
description and/or truncated title. Rebuilds the store's brand template
from fresh source characteristics + a word-safe title. Source-faithful, zero
LLM generation (same builder as the W3636 repair).

Not a hallucination rewrite — this fixes structurally-broken listings.

Usage:
  python scripts/repair_broken_listings.py --sku W6018P506376            # dry-run
  python scripts/repair_broken_listings.py --sku W6018P506376 --apply
  python scripts/repair_broken_listings.py --sku-file logs/repair_queue.txt --apply
"""
import argparse
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

for _n in ("stdout", "stderr"):
    _s = getattr(sys, _n)
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
DB = ROOT / "ebay_collection.db"
BACKUP_DIR = ROOT / "logs" / "repair_backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

from scripts.audit_fix_active_listings import (  # noqa: E402
    build_structured_description_from_source,
    compress_html,
    _put_inventory_product_only,
    _select_best_offer,
)
from src.services.source_refresh import build_source_snapshot  # noqa: E402
from src.utils.title_sanitizer import normalize_listing_title_for_ebay  # noqa: E402
from src.utils.claim_diff_engine import build_source_constraints, detect_claim_violations  # noqa: E402
from src.utils.store_profile import get_store_profile  # noqa: E402

_PROFILE = get_store_profile()
_BRAND_UPPER = _PROFILE.brand_name.upper()
TEMPLATE_MARKERS = (_BRAND_UPPER, _PROFILE.brand_tagline, "KEY FEATURES",
                    "SPECIFICATIONS", _PROFILE.description_footer_line1.strip("✦ "))


_ACRONYMS = {"mdf": "MDF", "pu": "PU", "pvc": "PVC", "led": "LED", "abs": "ABS",
             "hdpe": "HDPE", "tv": "TV", "usb": "USB", "pe": "PE", "pp": "PP",
             "eva": "EVA", "mgo": "MGO"}


def _material_case(raw: str) -> str:
    """Title-case a material but keep acronyms upper (MDF not Mdf)."""
    parts = re.split(r"([,+/&]| and | with )", str(raw or ""))
    out = []
    for part in parts:
        toks = [(_ACRONYMS.get(w.lower(), w.title())) for w in part.split()]
        out.append(" ".join(toks) if toks else part)
    return "".join(out).strip()


def _dajian():
    from src.clients.dajian_client import DaJianClient
    cid, sec = os.getenv("DAJIAN_API_KEY"), os.getenv("DAJIAN_API_SECRET")
    return DaJianClient(cid, sec) if cid and sec else None


def build_repair(conn, dj, sku):
    """Return (new_title, new_description, new_aspects, snapshot) or (None, reason)."""
    row = conn.execute(
        "SELECT title, optimization FROM collected_products WHERE sku=?", (sku,)
    ).fetchone()
    if not row:
        return None, "not_in_db"
    opt = json.loads(row[1] or "{}")
    aspects = dict(opt.get("aspects") or {})

    detail = dj.get_product_detail_by_sku(sku) if dj else None
    snap = build_source_snapshot(detail) if detail else None
    if snap is None:
        return None, "no_source"
    if not snap.characteristics:
        return None, "thin_source_characteristics"

    # Title: rebuild word-safe from source product name. A source name too thin
    # to be a title (GIGA sometimes stores just "chicken coop") must NOT abandon
    # the repair — the description is the serious defect here (a live listing
    # showing raw Chinese supplier data), and a short title is the lesser evil.
    # Keep the current live title in that case and still fix the description.
    # W3166P455683 sat broken on live because this bailed out (2026-07-27).
    new_title, _ = normalize_listing_title_for_ebay(snap.title, source_title=snap.title)
    if not new_title or len(new_title) < 15:
        current_title, _ = normalize_listing_title_for_ebay(
            re.sub(r"\s+", " ", str(row[0] or "")).strip(), source_title=snap.title
        )
        new_title = current_title or new_title
        if not new_title:
            return None, "title_rebuild_failed"

    # Material from source (fixes Wood→MDF style mismatches)
    if snap.attributes.get("Main Material"):
        aspects["Material"] = [_material_case(snap.attributes["Main Material"])]

    # Description: store template rebuilt from source characteristics
    features_html = (
        "<div><h3>Product Features</h3><ul>"
        + "".join(f"<li>{c}</li>" for c in snap.characteristics)
        + "</ul></div>"
    )
    new_desc = build_structured_description_from_source(
        new_title, features_html, snap.attributes, snap.specs, aspects
    )
    if not new_desc or not all(m in new_desc for m in (_BRAND_UPPER, "KEY FEATURES")):
        return None, "template_build_failed"
    return (new_title, new_desc, aspects, snap), None


def verify(new_title, new_desc, new_aspects, snap):
    cons = build_source_constraints(
        attrs=snap.attributes, specs=snap.specs,
        source_description=snap.description_html or "", source_title=snap.title,
    )
    cv = [v for v in detect_claim_violations(
        source_constraints=cons, generated_title=new_title,
        generated_description=new_desc, generated_aspects=new_aspects) if v.severity == "CRITICAL"]
    markers_ok = all(m in new_desc for m in TEMPLATE_MARKERS)
    lwh = bool(re.search(r"\d", new_desc))
    title_ok = len(new_title) <= 80 and not re.search(r"\b(with|for|and|a|of|&)\s*$", new_title, re.I)
    return {"claim_critical": len(cv), "markers_ok": markers_ok,
            "has_digits": lwh, "title_ok": title_ok,
            "passed": not cv and markers_ok and title_ok}


def repair_one(conn, dj, ebay, sku, apply):
    built, reason = build_repair(conn, dj, sku)
    if built is None:
        return {"sku": sku, "result": "SKIP", "reason": reason}
    new_title, new_desc, new_aspects, snap = built
    v = verify(new_title, new_desc, new_aspects, snap)
    rec = {"sku": sku, "new_title": new_title, "title_len": len(new_title),
           "desc_len": len(new_desc), "verify": v}
    if not v["passed"]:
        rec["result"] = "VERIFY_FAILED"
        return rec
    if not apply:
        rec["result"] = "DRY_OK"
        return rec

    # backup live before touching
    inv = ebay.get_inventory_item(sku) or {}
    p = inv.get("product") or {}
    offers = ebay.get_offers_by_sku(sku)
    best = _select_best_offer(offers)
    if not best or not best.get("offerId"):
        rec["result"] = "NO_OFFER"
        return rec
    (BACKUP_DIR / f"{sku}.json").write_text(json.dumps({
        "title": p.get("title"), "description": (best.get("listingDescription") or p.get("description")),
        "aspects": p.get("aspects"), "offer_id": best.get("offerId"),
        "listing_id": (best.get("listing") or {}).get("listingId"),
        "category_id": best.get("categoryId") or (best.get("category") or {}).get("categoryId"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    offer_id = best["offerId"]
    cat = best.get("categoryId") or (best.get("category") or {}).get("categoryId")
    _put_inventory_product_only(ebay, sku, new_title, new_desc, new_aspects)
    if not ebay.update_offer_category(offer_id, cat, listing_description=compress_html(new_desc)):
        rec["result"] = "OFFER_UPDATE_FAILED"
        return rec
    pub = ebay.publish_offer(offer_id)
    lid = (pub or {}).get("listingId")
    if not lid:
        rec["result"] = "PUBLISH_FAILED"
        return rec

    # live verify
    live = ebay.get_inventory_item(sku) or {}
    lp = live.get("product") or {}
    off2 = ebay.get_offers_by_sku(sku)
    ld = (off2[0].get("listingDescription") if off2 else "") or lp.get("description") or ""
    live_ok = (_BRAND_UPPER in ld and "产品规格" not in ld
               and not re.search(r"\b(with|for|and|a|of|&)\s*$", lp.get("title", ""), re.I))
    rec["result"] = "DONE" if live_ok else "LIVE_VERIFY_FAILED"
    rec["listing_id"] = lid

    if live_ok:
        r = conn.execute("SELECT optimization, logs FROM collected_products WHERE sku=?", (sku,)).fetchone()
        opt = json.loads(r[0] or "{}")
        opt["title"] = new_title
        opt["description"] = compress_html(new_desc)
        opt["aspects"] = new_aspects
        logs = json.loads(r[1] or "[]")
        logs.append(f"Broken-listing repair at {datetime.now().isoformat()}: "
                    f"rebuilt template + word-safe title from source (was raw-source/truncated).")
        conn.execute(
            "UPDATE collected_products SET optimization=?, logs=?, updated_at=CURRENT_TIMESTAMP WHERE sku=?",
            (json.dumps(opt, ensure_ascii=False), json.dumps(logs, ensure_ascii=False), sku))
        conn.commit()
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sku", action="append")
    ap.add_argument("--sku-file")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    skus = list(args.sku or [])
    if args.sku_file:
        skus += [l.strip() for l in open(args.sku_file, encoding="utf-8") if l.strip()]
    skus = list(dict.fromkeys(skus))
    if args.limit:
        skus = skus[: args.limit]

    dj = _dajian()
    if dj is None:
        print("[ERROR] no Dajian creds"); return 1
    from src.clients.real_ebay_client import create_real_ebay_client
    ebay = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    conn = sqlite3.connect(DB)

    from collections import Counter
    tally = Counter()
    out = []
    for sku in skus:
        try:
            rec = repair_one(conn, dj, ebay, sku, args.apply)
        except Exception as e:
            rec = {"sku": sku, "result": "ERROR", "reason": str(e)[:80]}
        tally[rec["result"]] += 1
        out.append(rec)
        v = rec.get("verify", {})
        print(f"{sku}: {rec['result']}"
              + (f" title_len={rec.get('title_len')} claim={v.get('claim_critical')} tpl={v.get('markers_ok')}"
                 if "verify" in rec else f" ({rec.get('reason','')})"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    (ROOT / "logs" / f"repair_run_{ts}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nTALLY:", dict(tally))
    return 0


if __name__ == "__main__":
    sys.exit(main())
