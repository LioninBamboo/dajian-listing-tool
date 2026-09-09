#!/usr/bin/env python3
"""Spot-check live W714 listings: banner dedupe, material, capacity, quantity."""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.db.collection_db import SessionLocal
from src.db.collection_models import CollectedProduct
from src.services.ebay_auth import EbayOAuthService


def published_w714_skus() -> list[str]:
    skus: list[str] = []
    for b in ("B0", "B1", "B2", "B3", "B4"):
        p = ROOT / "tools" / "w714_listing" / f"{b}_skus.txt"
        skus.extend(p.read_text(encoding="utf-8").split())
    db = SessionLocal()
    try:
        out = []
        for sku in skus:
            r = db.query(CollectedProduct).filter_by(sku=sku).first()
            if r and r.status == "PUBLISHED":
                out.append(sku)
        return out
    finally:
        db.close()


def check_one(sku: str, token: str) -> dict:
    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }
    base = "https://api.ebay.com"
    row: dict = {"sku": sku, "ok": True, "issues": []}

    inv = requests.get(
        f"{base}/sell/inventory/v1/inventory_item/{sku}",
        headers=h,
        timeout=30,
        verify=False,
    )
    qty = None
    if inv.status_code == 200:
        qty = (
            ((inv.json().get("availability") or {}).get("shipToLocationAvailability") or {}).get(
                "quantity"
            )
        )
    row["quantity"] = qty
    if qty is None:
        row["issues"].append("missing_quantity")
        row["ok"] = False
    elif int(qty) <= 0:
        row["issues"].append("quantity_zero")
        row["ok"] = False

    offers = requests.get(
        f"{base}/sell/inventory/v1/offer?sku={sku}",
        headers=h,
        timeout=30,
        verify=False,
    )
    if offers.status_code != 200 or not offers.json().get("offers"):
        row["issues"].append("no_offer")
        row["ok"] = False
        return row
    offer = sorted(
        offers.json()["offers"],
        key=lambda o: (0 if o.get("status") == "PUBLISHED" else 1),
    )[0]
    listing = offer.get("listing") or {}
    row["listing_id"] = listing.get("listingId")
    row["listing_status"] = listing.get("listingStatus")
    if listing.get("listingStatus") not in ("ACTIVE", None):
        # OUT_OF_STOCK still published but flagged
        if listing.get("listingStatus") == "OUT_OF_STOCK":
            row["issues"].append("listing_out_of_stock")
            row["ok"] = False

    detail = requests.get(
        f"{base}/sell/inventory/v1/offer/{offer['offerId']}",
        headers=h,
        timeout=30,
        verify=False,
    )
    desc = ""
    aspects = {}
    if detail.status_code == 200:
        body = detail.json()
        desc = body.get("listingDescription") or ""
        # inventory aspects live on inventory item
    inv2 = inv.json() if inv.status_code == 200 else {}
    aspects = ((inv2.get("product") or {}).get("aspects")) or {}

    aquaverve = desc.upper().count("AQUAVERVE")
    row["aquaverve_count"] = aquaverve
    if aquaverve != 1:
        row["issues"].append(f"banner_count={aquaverve}")
        row["ok"] = False
    if "ships from" not in desc.lower():
        row["issues"].append("missing_footer")
        row["ok"] = False

    mat_blob = " ".join(
        str(v)
        for k, v in aspects.items()
        if "material" in k.lower() or "upholstery" in k.lower()
    ).lower()
    row["materials"] = mat_blob[:120]
    banned = [w for w in ("foam", "engineered wood", "hardwood", " metal") if w.strip() in mat_blob]
    # also scan description lightly
    desc_l = desc.lower()
    for w in ("engineered wood", "high-density foam"):
        if w in desc_l:
            banned.append(w)
    if banned:
        row["issues"].append("banned_material:" + ",".join(sorted(set(banned))))
        row["ok"] = False

    # capacity wild ranges like 3-4
    if re.search(r"\b\d\s*[-–]\s*\d\s*person\b", desc, flags=re.I):
        row["issues"].append("capacity_range_in_description")
        row["ok"] = False

    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json-out", type=Path, default=ROOT / "tools/w714_listing/spot_check_report.json")
    args = ap.parse_args()

    pool = published_w714_skus()
    rng = random.Random(args.seed)
    sample = pool if len(pool) <= args.sample else rng.sample(pool, args.sample)
    print(f"spot-check {len(sample)}/{len(pool)} published W714 SKUs", flush=True)

    oauth = EbayOAuthService(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    token = oauth.get_valid_token()
    rows = []
    for i, sku in enumerate(sample, 1):
        row = check_one(sku, token)
        rows.append(row)
        mark = "OK" if row["ok"] else "ISSUE"
        print(
            f"[{i}/{len(sample)}] {mark} {sku} qty={row.get('quantity')} "
            f"banner={row.get('aquaverve_count')} status={row.get('listing_status')} "
            f"issues={row.get('issues')}",
            flush=True,
        )

    bad = [r for r in rows if not r["ok"]]
    report = {
        "sampled": len(rows),
        "pool": len(pool),
        "ok": len(rows) - len(bad),
        "issues": len(bad),
        "rows": rows,
    }
    args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("SUMMARY", f"ok={report['ok']} issues={report['issues']} -> {args.json_out}", flush=True)
    return 0 if not bad else 2


if __name__ == "__main__":
    raise SystemExit(main())
