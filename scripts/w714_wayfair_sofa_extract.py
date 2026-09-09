#!/usr/bin/env python3
"""Extract Wayfair class-305 sofa fields for W714 combo SKUs with stock > 20.

Independent of the eBay publish pipeline. Reuses VL/DB caches when present,
scrapes GIGA public pages for title/color/material/images, and VL-fills
assembled dims once per layout family.

Output: tools/w714_listing/wayfair_sofas_305_extract.json
Optional copy: Aquawood Smart ERP-Wayfair/data/local_review/

  python scripts/w714_wayfair_sofa_extract.py
  python scripts/w714_wayfair_sofa_extract.py --limit 5
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from openai import OpenAI
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
W714 = ROOT / "tools" / "w714_listing"
CACHE = W714 / "cache"
OUT = W714 / "wayfair_sofas_305_extract.json"
WAYFAIR_COPY = Path(r"C:\Users\poonx\Aquawood Smart ERP-Wayfair\data\local_review\w714_combo_stock20_sofas_305_extract.json")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

SEARCH_URL = "https://www.gigab2b.com/index.php?route=product/search&search={sku}"

UPHOLSTERY_ENUM = {
    "chenille": "Chenille",
    "corduroy": "Corduroy",
    "velvet": "Velvet",
    "linen": "Linen",
    "microfiber": "Microfiber / Microsuede",
    "microsuede": "Microfiber / Microsuede",
    "polyester": "Polyester",
    "fabric": "Fabric",
    "faux leather": "Faux Leather",
    "leather": "Leather Match",
    "cotton": "Cotton",
}
COLOR_ENUM = {
    "blue": "Blue",
    "black": "Black",
    "brown": "Brown",
    "white": "White",
    "green": "Green",
    "gray": "Gray",
    "grey": "Gray",
    "red": "Red",
    "beige": "Beige",
    "yellow": "Yellow",
    "pink": "Pink",
    "orange": "Orange",
    "purple": "Purple",
    "cream": "Cream",
    "ivory": "Ivory",
    "tan": "Tan",
    "teal": "Teal",
    "navy": "Navy",
    "light coffee": "Brown",
    "coffee": "Brown",
    "camel": "Tan",
    "khaki": "Beige",
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_targets(min_stock: float = 20.0) -> list[dict]:
    rows = list(csv.DictReader((W714 / "candidates_under_400.csv").open(encoding="utf-8-sig")))
    out = []
    for r in rows:
        if str(r.get("is_combo")).lower() != "true":
            continue
        if (_num(r.get("stock")) or 0) <= min_stock:
            continue
        out.append(r)
    return out


def layout_key(row: dict) -> str:
    """Same series + box count + net weight share assembled footprint."""
    return f"{row['sheet']}|boxes={row.get('combo_box_count')}|net={row.get('net_weight_lbs')}|gross={row.get('gross_weight_lbs')}"


def accept_cookies(page) -> None:
    try:
        page.get_by_role("button", name=re.compile("Accept", re.I)).click(timeout=1000)
    except Exception:
        pass


def parse_page_dims(text: str) -> dict:
    dims = {}
    m = re.search(r"Product Dimensions\s*(.*?)\s*Package Size", text, re.S | re.I)
    if m:
        block = m.group(1)
        for lab, key in [
            ("Assembled Length (in.)", "assembled_length_in"),
            ("Assembled Width (in.)", "assembled_width_in"),
            ("Assembled Height (in.)", "assembled_height_in"),
            ("Product Weight (lbs.)", "product_weight_lbs"),
        ]:
            mm = re.search(re.escape(lab) + r"\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", block, re.I)
            if mm:
                dims[key] = float(mm.group(1))
    m2 = re.search(r"Package Size\s*(.*?)(?:\+1\s|About Us|Sign in|Headquarters)", text, re.S | re.I)
    if m2:
        block = m2.group(1)
        for lab, key in [
            ("Length (in.)", "package_length_in"),
            ("Width (in.)", "package_width_in"),
            ("Height (in.)", "package_height_in"),
            ("Weight (lbs.)", "package_weight_lbs"),
        ]:
            mm = re.search(re.escape(lab) + r"\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", block, re.I)
            if mm:
                dims[key] = float(mm.group(1))
    return dims


def scrape_sku(page, sku: str) -> dict:
    page.goto(SEARCH_URL.format(sku=sku), wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2200)
    accept_cookies(page)
    href = page.evaluate(
        """(sku) => {
        const links=[...document.querySelectorAll('a[href*="product_id="]')];
        for (const a of links) {
          const t=((a.closest('div')||a).innerText||'');
          if (t.includes(sku)) return a.href || a.getAttribute('href');
        }
        return links[0] ? (links[0].href || links[0].getAttribute('href')) : null;
      }""",
        sku,
    )
    if not href:
        raise RuntimeError(f"{sku}: no product link")
    if not href.startswith("http"):
        href = "https://www.gigab2b.com/" + href.lstrip("/")
    page.goto(href, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(3800)
    text = page.inner_text("body")
    title = ""
    try:
        title = (page.locator("h1").first.inner_text(timeout=2000) or "").strip()
    except Exception:
        pass
    if len(title) < 8:
        pm = re.search(r"Product Name:\s*(.+)", text)
        title = pm.group(1).strip() if pm else sku
    color_m = re.search(r"Main Color:\s*(.+)", text)
    mat_m = re.search(r"Main Material:\s*(.+)", text)
    seats_m = re.search(r"Seats:\s*(.+)", text)
    origin_m = re.search(r"Place of Origin:\s*(.+)", text) or re.search(r"Country of Origin:\s*(.+)", text)
    images = page.evaluate(
        """() => [...new Set([...document.querySelectorAll('img')]
          .map(i => i.src || '')
          .filter(s => s.includes('/image/wkseller/'))
          .map(s => s.split('&x-oss')[0]))].slice(0,24)"""
    )
    return {
        "sku": sku,
        "title": title,
        "url": href,
        "main_color": (color_m.group(1).strip() if color_m else ""),
        "main_material": (mat_m.group(1).strip() if mat_m else ""),
        "seats_raw": (seats_m.group(1).strip() if seats_m else ""),
        "country_raw": (origin_m.group(1).strip() if origin_m else ""),
        "dims": parse_page_dims(text),
        "images": images,
        "text": text,
    }


def extract_json_obj(text: str) -> dict | None:
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def vl_dims(sku: str, images: list[str], client: OpenAI | None) -> dict:
    cache_path = CACHE / f"vl_{sku}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("Assembled Length (in.)"):
                return {
                    "assembled_length_in": float(cached["Assembled Length (in.)"]),
                    "assembled_width_in": float(cached["Assembled Width (in.)"]),
                    "assembled_height_in": float(cached["Assembled Height (in.)"]),
                    "_source": "vl_cache",
                }
        except Exception:
            pass
    if not client or not images:
        return {}
    prompt = (
        "Is this a furniture dimension/size diagram with numeric overall L/W/H? "
        "If yes, return JSON "
        '{"is_dimension_diagram":true,"length":number,"width":number,"height":number,"unit":"inches"}. '
        'Else {"is_dimension_diagram":false}.'
    )
    prefer = list(range(3, min(len(images), 10))) + list(range(0, min(3, len(images))))
    for idx in prefer:
        url = images[idx]
        try:
            r = requests.get(url, timeout=25)
            if r.status_code != 200 or len(r.content) < 8000 or len(r.content) > 1_800_000:
                continue
            b64 = base64.b64encode(r.content).decode()
            resp = client.chat.completions.create(
                model="qwen-vl-plus",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        ],
                    }
                ],
                max_tokens=250,
            )
            data = extract_json_obj(resp.choices[0].message.content or "") or {}
            if data.get("is_dimension_diagram") and data.get("length") and data.get("width") and data.get("height"):
                result = {
                    "Assembled Length (in.)": str(data["length"]),
                    "Assembled Width (in.)": str(data["width"]),
                    "Assembled Height (in.)": str(data["height"]),
                }
                CACHE.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
                print(f"    VL {sku} #{idx}: {data['length']}x{data['width']}x{data['height']}", flush=True)
                return {
                    "assembled_length_in": float(data["length"]),
                    "assembled_width_in": float(data["width"]),
                    "assembled_height_in": float(data["height"]),
                    "_source": "vl",
                }
        except Exception as exc:
            print(f"    VL skip {sku}#{idx}: {exc}", flush=True)
    return {}


def map_upholstery(raw: str) -> tuple[str | None, str | None]:
    raw = (raw or "").strip()
    if not raw:
        return None, None
    low = raw.lower()
    for k, v in UPHOLSTERY_ENUM.items():
        if k in low:
            # Chenille etc. are official; also keep Fabric as parent when only fabric-like
            return v, raw
    return None, raw


def map_color(raw: str, title: str = "") -> tuple[str | None, str | None]:
    blob = f"{raw} {title}".lower()
    raw = (raw or "").strip() or None
    for k, v in COLOR_ENUM.items():
        if k in blob:
            return v, raw
    return None, raw


def infer_product_type_and_shape(title: str, seats_raw: str, box_count: int, child_combo: str) -> tuple[str, str | None, int | None]:
    t = (title or "").lower()
    seats = None
    sm = re.search(r"(\d+)\s*-?\s*seat", f"{seats_raw} {title}", re.I)
    if sm:
        seats = int(sm.group(1))
    # name codes like 2S+1O / 3S
    if seats is None:
        m = re.search(r"(\d+)\s*s\b", title, re.I)
        if m:
            seats = int(m.group(1))
    shape = None
    ptype = "Sectional"
    if "u-shaped" in t or "u shaped" in t:
        shape = "U-Shaped"
        ptype = "Sectional"
    elif "l-shaped" in t or "l shaped" in t or "chaise" in t or "ottoman" in t:
        shape = "L-Shaped"
        ptype = "Sectional" if ("sectional" in t or "ottoman" in t or "chaise" in t or box_count >= 2) else "Sofa & Chaise"
    elif "loveseat" in t:
        ptype = "Loveseat"
        shape = "Rectangle"
    elif box_count >= 2:
        ptype = "Sectional"
        shape = "L-Shaped"
    else:
        ptype = "Sofa"
        shape = "Rectangle"
    if seats is None and box_count:
        # rough: 2-box ~2 seat, 3-box ~3, 4-box ~4
        seats = min(6, max(2, box_count))
    return ptype, shape, seats


def pieces_included(child_combo: str, title: str) -> str:
    blob = f"{child_combo} {title}".lower()
    if "ottoman" in blob or "ottman" in blob or re.search(r"\+\s*\d*o\b", blob):
        # Has ottoman component — use official enum
        return "Ottoman"
    return "Does Not Apply"


def db_enrich(sku: str) -> dict:
    try:
        from src.db.collection_db import SessionLocal
        from src.db.collection_models import CollectedProduct

        db = SessionLocal()
        try:
            r = db.query(CollectedProduct).filter_by(sku=sku).first()
            if not r:
                return {}
            a = r.attributes or {}
            s = r.specs or {}
            out = {
                "title": r.title,
                "images": r.images or [],
                "price": r.price,
                "shipping": r.shipping,
            }
            if a.get("Assembled Length (in.)"):
                out["assembled_length_in"] = float(a["Assembled Length (in.)"])
                out["assembled_width_in"] = float(a["Assembled Width (in.)"])
                out["assembled_height_in"] = float(a["Assembled Height (in.)"])
            if a.get("Product Weight (lbs.)"):
                out["product_weight_lbs"] = float(a["Product Weight (lbs.)"])
            if s.get("Package Length (in.)"):
                out["package_length_in"] = float(s["Package Length (in.)"])
                out["package_width_in"] = float(s.get("Package Width (in.)") or 0) or None
                out["package_height_in"] = float(s.get("Package Height (in.)") or 0) or None
            if s.get("Package Weight (lbs.)"):
                out["package_weight_lbs"] = float(s["Package Weight (lbs.)"])
            if a.get("Color") or a.get("Main Color"):
                out["main_color"] = a.get("Color") or a.get("Main Color")
            if a.get("Material") or a.get("Main Material"):
                out["main_material"] = a.get("Material") or a.get("Main Material")
            return out
        finally:
            db.close()
    except Exception:
        return {}


def build_record(row: dict, scraped: dict, layout_dims: dict) -> dict:
    title = scraped.get("title") or row.get("name") or row["sku"]
    material_raw = scraped.get("main_material") or ""
    color_raw = scraped.get("main_color") or ""
    uph, uph_raw = map_upholstery(material_raw)
    if not uph and "chenille" in title.lower():
        uph, uph_raw = "Chenille", "Chenille"
    color, color_raw_out = map_color(color_raw, title)
    box_count = int(float(row.get("combo_box_count") or 1))
    ptype, shape, seats = infer_product_type_and_shape(
        title, scraped.get("seats_raw") or "", box_count, row.get("child_combo") or ""
    )
    # Prefer excel net/gross for weights
    product_weight = _num(row.get("net_weight_lbs")) or scraped.get("product_weight_lbs") or layout_dims.get("product_weight_lbs")
    package_weight = _num(row.get("gross_weight_lbs")) or scraped.get("package_weight_lbs") or layout_dims.get("package_weight_lbs")
    al = layout_dims.get("assembled_length_in") or scraped.get("assembled_length_in")
    aw = layout_dims.get("assembled_width_in") or scraped.get("assembled_width_in")
    ah = layout_dims.get("assembled_height_in") or scraped.get("assembled_height_in")
    pl = scraped.get("package_length_in") or layout_dims.get("package_length_in")
    pw = scraped.get("package_width_in") or layout_dims.get("package_width_in")
    ph = scraped.get("package_height_in") or layout_dims.get("package_height_in")
    base_cost = _num(row.get("table_discount_price"))
    country = "China"
    if scraped.get("country_raw") and "china" in scraped["country_raw"].lower():
        country = "China"

    # Wayfair axis remap
    wayfair_width = al  # GIGA Length → Wayfair Width
    wayfair_depth = aw  # GIGA Width → Wayfair Depth
    wayfair_height = ah

    return {
        "sku": row["sku"],
        "title": title,
        "giga_url": scraped.get("url") or "",
        "sheet": row.get("sheet"),
        "batch": row.get("batch"),
        "stock": _num(row.get("stock")),
        "child_combo": row.get("child_combo") or "",
        "combo_box_count": box_count,
        "class_id": 305,
        "wayfair_category": "Sofas / Loveseats",
        "brand": "AquaWood",
        "supplier_part_number": row["sku"],
        "manufacturer_part_number": row["sku"],
        "product_type": ptype,
        "upholstery_material": uph,
        "upholstery_material_raw": uph_raw or material_raw or None,
        "upholstery_color": color,
        "upholstery_color_raw": color_raw_out or color_raw or None,
        "frame_material": None,  # not asserted on page — do not invent
        "fill_material": None,
        "cushion_construction": None,
        "overall_shape": shape,
        "seating_capacity": seats,
        "assembled_length_in": al,
        "assembled_width_in": aw,
        "assembled_height_in": ah,
        "wayfair_overall_width_in": wayfair_width,
        "wayfair_overall_depth_in": wayfair_depth,
        "wayfair_overall_height_in": wayfair_height,
        "product_weight_lbs": product_weight,
        "package_length_in": pl,
        "package_width_in": pw,
        "package_height_in": ph,
        "package_weight_lbs": package_weight,
        "country_of_origin": country,
        "assembly": "Full Assembly Needed - Additional Tools Required and Not Included"
        if box_count >= 2
        else "Assembly Needed",
        "leg_color_finish": None,
        "is_leather": False,
        "leather_type": "Does Not Apply",
        "leather_match": "Does Not Apply",
        "mattress_included": False,
        "pieces_included": pieces_included(row.get("child_combo") or "", title),
        "base_cost": base_cost,
        "shipping_cost_ref": _num(row.get("table_shipping")),
        "minimum_order_quantity": 1,
        "force_quantity_multiplier": 1,
        "display_set_quantity": 1,
        "ship_type": "LTL" if (package_weight or 0) >= 150 or box_count >= 2 else "Small Parcel",
        "lead_time": "48",
        "replacement_lead_time": "48",
        "warning_required": "No",
        "supplier_intended_and_approved_use": "Residential Use",
        "soffa_compliant": "No",
        "california_ab_1817_ca_pfas": "No",
        "contains_flame_retardant_materials": "No",
        "cwp": "No",
        "carb_phase_ii": "Does Not Apply",
        "tsca_title_vi": "Does Not Apply",
        "canfer": "Does Not Apply",
        "images": scraped.get("images") or [],
        "dims_source": layout_dims.get("_source") or scraped.get("dims_source") or "page_or_excel",
        "extract_notes": [],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-stock", type=float, default=20.0)
    ap.add_argument("--no-vl", action="store_true")
    ap.add_argument("--sku", action="append", default=[])
    args = ap.parse_args()

    targets = load_targets(args.min_stock)
    if args.sku:
        wanted = set(args.sku)
        targets = [r for r in targets if r["sku"] in wanted]
    if args.limit:
        targets = targets[: args.limit]
    print(f"targets: {len(targets)} combo SKUs stock>{args.min_stock}", flush=True)

    client = None
    if not args.no_vl and os.getenv("QWEN_API_KEY"):
        client = OpenAI(api_key=os.getenv("QWEN_API_KEY"), base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")

    # Group by layout for one VL per family
    by_layout: dict[str, list[dict]] = {}
    for r in targets:
        by_layout.setdefault(layout_key(r), []).append(r)

    layout_dims_map: dict[str, dict] = {}
    records = []
    scraped_cache: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()

        # First pass: scrape one representative per layout for dims + all SKUs for color/images
        for i, row in enumerate(targets, 1):
            sku = row["sku"]
            lk = layout_key(row)
            print(f"[{i}/{len(targets)}] {sku} layout={lk}", flush=True)
            merged = {"sku": sku}
            dbinfo = db_enrich(sku)
            merged.update({k: v for k, v in dbinfo.items() if v not in (None, "", [])})

            need_scrape = not merged.get("images") or not merged.get("title") or not merged.get("main_material")
            if need_scrape or lk not in layout_dims_map:
                try:
                    scraped = scrape_sku(page, sku)
                    scraped_cache[sku] = scraped
                    for k in ("title", "main_color", "main_material", "seats_raw", "country_raw", "url", "images"):
                        if scraped.get(k) and not merged.get(k):
                            merged[k] = scraped[k]
                    merged.update({k: v for k, v in (scraped.get("dims") or {}).items() if v})
                    time.sleep(0.8)
                except Exception as exc:
                    print(f"  scrape fail: {exc}", flush=True)

            # Resolve layout dims once
            if lk not in layout_dims_map:
                dims = {}
                if merged.get("assembled_length_in"):
                    dims = {
                        "assembled_length_in": merged["assembled_length_in"],
                        "assembled_width_in": merged["assembled_width_in"],
                        "assembled_height_in": merged["assembled_height_in"],
                        "_source": "db_or_page",
                    }
                if not dims.get("assembled_length_in"):
                    dims = vl_dims(sku, merged.get("images") or [], client) or {}
                # package from scrape/db
                for pk in ("package_length_in", "package_width_in", "package_height_in", "package_weight_lbs"):
                    if merged.get(pk):
                        dims[pk] = merged[pk]
                layout_dims_map[lk] = dims
                print(f"  layout dims: {dims}", flush=True)

            # If this SKU still missing images/title, light scrape already done
            if not merged.get("images") and sku in scraped_cache:
                merged["images"] = scraped_cache[sku].get("images") or []

            rec = build_record(row, merged, layout_dims_map.get(lk) or {})
            if not rec.get("assembled_length_in"):
                rec["extract_notes"].append("missing_assembled_dims")
            if not rec.get("upholstery_material"):
                rec["extract_notes"].append("missing_upholstery_material_enum")
            if not rec.get("upholstery_color"):
                rec["extract_notes"].append("missing_upholstery_color_enum")
            if not rec.get("images"):
                rec["extract_notes"].append("missing_images")
            records.append(rec)

        browser.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    # CSV summary for quick review
    csv_path = W714 / "wayfair_sofas_305_extract_summary.csv"
    fields = [
        "sku", "stock", "product_type", "overall_shape", "seating_capacity",
        "upholstery_material", "upholstery_color", "wayfair_overall_width_in",
        "wayfair_overall_depth_in", "wayfair_overall_height_in", "product_weight_lbs",
        "package_weight_lbs", "base_cost", "pieces_included", "dims_source", "extract_notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["extract_notes"] = "|".join(r.get("extract_notes") or [])
            w.writerow(row)

    try:
        WAYFAIR_COPY.parent.mkdir(parents=True, exist_ok=True)
        WAYFAIR_COPY.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"copied -> {WAYFAIR_COPY}", flush=True)
    except Exception as exc:
        print(f"copy skipped: {exc}", flush=True)

    missing = sum(1 for r in records if "missing_assembled_dims" in (r.get("extract_notes") or []))
    print(f"wrote {len(records)} records -> {OUT} (missing_dims={missing})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
