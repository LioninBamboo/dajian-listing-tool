#!/usr/bin/env python3
"""Collect a list of GigaCloud SKUs into GrovePop's collected_products as COLLECTED,
mapping detail fields (dims/material/color/price/media) the way the extension+enrichment
path would. After this, run batch_analyze.py (garden optimize -> READY) then
batch_publish.py. Idempotent: updates existing rows, never duplicates.

    python scripts/pilot_collect_garden.py --skus-file <path-to-json-list>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.clients.dajian_client import DaJianClient
from src.db.collection_db import SessionLocal
from src.db.collection_models import CollectedProduct


def _num(v):
    try:
        f = float(v)
        return None if f <= 0 else f
    except (TypeError, ValueError):
        return None


def map_detail(detail: dict, price: float | None) -> dict:
    attrs = dict(detail.get("attributes") or {}) if isinstance(detail.get("attributes"), dict) else {}
    specs = {}
    # Package dims -> specs (eBay shipping); assembled dims -> attributes (display)
    for k, s in [("length", "Package Length (in.)"), ("width", "Package Width (in.)"),
                 ("height", "Package Height (in.)"), ("weight", "Package Weight (lbs.)")]:
        v = _num(detail.get(k))
        if v:
            specs[s] = str(v)
    for k, a in [("assembledLength", "Assembled Length (in.)"), ("assembledWidth", "Assembled Width (in.)"),
                 ("assembledHeight", "Assembled Height (in.)")]:
        v = _num(detail.get(k))
        if v:
            attrs.setdefault(a, str(v))
    pw = _num(detail.get("assembledWeight"))
    if pw:
        attrs.setdefault("Product Weight (lbs.)", str(pw))
    if detail.get("mainMaterial"):
        attrs.setdefault("Material", str(detail["mainMaterial"]))
    if detail.get("mainColor"):
        attrs.setdefault("Color", str(detail["mainColor"]))
    # eBay uses imageUrls[0] as the gallery main image; GigaCloud's imageUrls order
    # often leads with a detail/lifestyle shot, so force mainImageUrl to the front.
    imgs = [u for u in (detail.get("imageUrls") or []) if u]
    main = detail.get("mainImageUrl")
    if main:
        imgs = [main] + [u for u in imgs if u != main]
    imgs = imgs[:24]
    vids = [v for v in ([detail.get("productVideoUrl")] + list(detail.get("videoUrls") or [])) if v]
    return {
        "title": detail.get("productName", "") or "",
        "description": detail.get("description", "") or "",
        "attributes": attrs, "specs": specs,
        "images": imgs, "videos": vids,
        "price": price or 0.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus-file", required=True)
    args = ap.parse_args()
    skus = json.load(open(args.skus_file, encoding="utf-8"))
    print(f"collecting {len(skus)} skus")

    c = DaJianClient(os.getenv("DAJIAN_API_KEY"), os.getenv("DAJIAN_API_SECRET"))
    details = {d["sku"]: d for d in (c.get_product_details(skus) or []) if isinstance(d, dict) and d.get("sku")}
    prices = {}
    try:
        for p in (c.get_product_prices(skus) or []):
            if isinstance(p, dict) and p.get("sku"):
                prices[p["sku"]] = _num(p.get("price") or p.get("sellPrice") or p.get("sellingPrice"))
    except Exception as e:
        print("price fetch warn:", e)

    db = SessionLocal()
    done = 0
    for sku in skus:
        det = details.get(sku)
        if not det:
            print(f"  {sku}: no detail, skip"); continue
        m = map_detail(det, prices.get(sku))
        row = db.query(CollectedProduct).filter_by(sku=sku).first()
        if row:
            row.title, row.description = m["title"], m["description"]
            row.attributes, row.specs = m["attributes"], m["specs"]
            row.images, row.videos = m["images"], m["videos"]
            row.price, row.shipping, row.stock = m["price"], 0.0, 10
            row.status = "COLLECTED"
            for f in ("attributes", "specs", "images", "videos"):
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(row, f)
        else:
            db.add(CollectedProduct(
                sku=sku, title=m["title"], price=m["price"], shipping=0.0, stock=10,
                description=m["description"], images=m["images"], videos=m["videos"],
                attributes=m["attributes"], specs=m["specs"], url="",
                status="COLLECTED", logs=["pilot_collect_garden"],
            ))
        db.commit()
        done += 1
        print(f"  {sku}: COLLECTED  price=${m['price']} imgs={len(m['images'])} "
              f"dims={'Y' if m['attributes'].get('Assembled Length (in.)') else 'N'}  {m['title'][:40]}")
    db.close()
    print(f"done: {done} collected")


if __name__ == "__main__":
    main()
