#!/usr/bin/env python3
"""Collect W714 SKUs from public GIGA product pages into the main-store DB.

- Title/images/specs from GIGA product page (no login required for specs/images)
- Cost: sheet 单买折扣价 + 快递费 (page exclusive / fulfillment when login blocked)
- Weights: Excel 净重 → Product Weight; Excel 毛重 → Package Weight
- Assembled LWH: page specs, else Qwen-VL on dimension diagrams
- Package LWH: page specs, else max carton among child piece pages

Usage:
  python scripts/w714_page_collect.py --batch B0
  python scripts/w714_page_collect.py --batch B1 --skip-existing
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
W714_DIR = ROOT / "tools" / "w714_listing"
CACHE_DIR = W714_DIR / "cache"
DIM_IMG_DIR = W714_DIR / "dim_imgs"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.db.collection_db import SessionLocal
from src.db.collection_models import CollectedProduct
from sqlalchemy.orm.attributes import flag_modified

SEARCH_URL = "https://www.gigab2b.com/index.php?route=product/search&search={sku}"


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_candidates(batch: str | None, sku_list: Path | None) -> list[dict]:
    rows = list(csv.DictReader((W714_DIR / "candidates_under_400.csv").open(encoding="utf-8-sig")))
    if batch:
        rows = [r for r in rows if r["batch"] == batch]
    if sku_list:
        wanted = {line.strip() for line in sku_list.read_text(encoding="utf-8").splitlines() if line.strip()}
        rows = [r for r in rows if r["sku"] in wanted]
    return rows


def parse_dims(text: str) -> dict:
    dims: dict[str, str] = {}
    m = re.search(r"Product Dimensions\s*(.*?)\s*Package Size", text, re.S | re.I)
    if m:
        block = m.group(1)
        for lab in (
            "Assembled Length (in.)",
            "Assembled Width (in.)",
            "Assembled Height (in.)",
            "Product Weight (lbs.)",
        ):
            mm = re.search(re.escape(lab) + r"\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", block, re.I)
            if mm:
                dims[lab] = mm.group(1)
    m2 = re.search(r"Package Size\s*(.*?)(?:\+1\s|About Us|Sign in|Headquarters)", text, re.S | re.I)
    if m2:
        block = m2.group(1)
        mapping = [
            ("Length (in.)", "Package Length (in.)"),
            ("Width (in.)", "Package Width (in.)"),
            ("Height (in.)", "Package Height (in.)"),
            ("Weight (lbs.)", "Package Weight (lbs.)"),
        ]
        for lab, key in mapping:
            mm = re.search(re.escape(lab) + r"\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", block, re.I)
            if mm:
                dims[key] = mm.group(1)
    return dims


def accept_cookies(page) -> None:
    try:
        page.get_by_role("button", name=re.compile("Accept", re.I)).click(timeout=1200)
    except Exception:
        pass


def resolve_product_url(page, sku: str) -> str:
    page.goto(SEARCH_URL.format(sku=sku), wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2800)
    accept_cookies(page)
    page.wait_for_timeout(800)
    # Prefer a card that mentions the SKU text
    href = page.evaluate(
        """(sku) => {
        const links = [...document.querySelectorAll('a[href*="product_id="]')];
        for (const a of links) {
          const card = a.closest('div') || a.parentElement;
          const txt = (card && card.innerText) || a.innerText || '';
          if (txt.includes(sku)) return a.getAttribute('href');
        }
        return links[0] ? links[0].getAttribute('href') : null;
      }""",
        sku,
    )
    if not href:
        # retry once
        page.wait_for_timeout(2500)
        href = page.locator('a[href*="product_id="]').first.get_attribute("href")
    if not href:
        raise RuntimeError(f"{sku}: product link not found")
    if not href.startswith("http"):
        href = "https://www.gigab2b.com/" + href.lstrip("/")
    return href


def scrape_product_page(page, sku: str, url: str | None = None) -> dict:
    href = url or resolve_product_url(page, sku)
    page.goto(href, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(4200)
    accept_cookies(page)
    text = page.inner_text("body")
    title = ""
    try:
        title = (page.locator("h1").first.inner_text(timeout=2000) or "").strip()
    except Exception:
        title = ""
    if len(title) < 8:
        pm = re.search(r"Product Name:\s*(.+)", text)
        title = pm.group(1).strip() if pm else sku
    dims = parse_dims(text)
    images = page.evaluate(
        """() => {
        const urls = [];
        document.querySelectorAll('img').forEach(img => {
          const s = img.src || img.getAttribute('data-src') || '';
          if (s.includes('/image/wkseller/')) urls.push(s.split('&x-oss')[0]);
        });
        return [...new Set(urls)];
      }"""
    )
    images = [u for u in images if "wkseller" in u][:24]
    ptype_m = re.search(r"Product Type\s*[:：]?\s*(.+)", text)
    product_type = ptype_m.group(1).strip() if ptype_m else ""
    color_m = re.search(r"Main Color:\s*(.+)", text)
    material_m = re.search(r"Main Material:\s*(.+)", text)
    bits = []
    if color_m:
        bits.append(f"Main Color: {color_m.group(1).strip()}")
    if material_m:
        bits.append(f"Main Material: {material_m.group(1).strip()}")
    if product_type:
        bits.append(f"Product Type: {product_type}")
    description = "<div><p>" + "</p><p>".join(bits) + "</p></div>" if bits else ""
    return {
        "sku": sku,
        "title": title,
        "url": href,
        "dims": dims,
        "images": images,
        "description": description,
        "product_type": product_type,
    }


def child_skus_from_combo(combo: str) -> list[str]:
    if not combo:
        return []
    return list(dict.fromkeys(re.findall(r"(W714P\d+)", str(combo), flags=re.I)))


def package_dims_from_children(page, child_skus: list[str], cache: dict) -> dict:
    """Use the largest carton among children as package L/W/H."""
    best = {"Package Length (in.)": 0.0, "Package Width (in.)": 0.0, "Package Height (in.)": 0.0}
    for csku in child_skus:
        if csku in cache:
            dims = cache[csku]
        else:
            try:
                scraped = scrape_product_page(page, csku)
                dims = scraped.get("dims") or {}
                cache[csku] = dims
                time.sleep(0.8)
            except Exception as exc:
                print(f"    child {csku} fail: {exc}")
                continue
        vals = [
            _num(dims.get("Package Length (in.)")),
            _num(dims.get("Package Width (in.)")),
            _num(dims.get("Package Height (in.)")),
        ]
        if not all(vals):
            continue
        # prefer largest volume carton
        vol = vals[0] * vals[1] * vals[2]
        cur = best["Package Length (in.)"] * best["Package Width (in.)"] * best["Package Height (in.)"]
        if vol > cur:
            best = {
                "Package Length (in.)": vals[0],
                "Package Width (in.)": vals[1],
                "Package Height (in.)": vals[2],
            }
    if best["Package Length (in.)"] <= 0:
        return {}
    return {k: str(v) for k, v in best.items()}


def extract_json_obj(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def vl_assembled_from_images(sku: str, image_urls: list[str], client: OpenAI | None) -> dict:
    """Scan gallery images with Qwen-VL until a dimension diagram is found."""
    if not client or not image_urls:
        return {}
    cache_path = CACHE_DIR / f"vl_{sku}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("Assembled Length (in.)"):
                return cached
        except Exception:
            pass

    outdir = DIM_IMG_DIR / sku
    outdir.mkdir(parents=True, exist_ok=True)
    prompt = (
        "Is this a furniture dimension/size diagram with numeric overall L/W/H? "
        "If yes, extract overall assembled footprint in inches as JSON: "
        '{"is_dimension_diagram":true,"length":number,"width":number,"height":number,'
        '"unit":"inches","notes":"..."}. '
        'If not, return {"is_dimension_diagram":false}.'
    )
    # Dimension diagrams are usually mid-gallery (often #3-#7), not the first lifestyle shots.
    ordered = list(image_urls)
    prefer = list(range(3, min(len(ordered), 8))) + list(range(0, min(3, len(ordered)))) + list(
        range(8, min(len(ordered), 12))
    )
    seen_idx: set[int] = set()
    for idx in prefer:
        if idx in seen_idx or idx >= len(ordered):
            continue
        seen_idx.add(idx)
        url = ordered[idx]
        try:
            r = requests.get(url, timeout=25)
            if r.status_code != 200 or len(r.content) < 8000:
                continue
            # Skip huge lifestyle photos (>1.8MB) — diagrams are usually smaller.
            if len(r.content) > 1_800_000:
                continue
            fp = outdir / f"{idx}.jpg"
            fp.write_bytes(r.content)
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
                max_tokens=350,
            )
            content = resp.choices[0].message.content or ""
            data = extract_json_obj(content) or {}
            if data.get("is_dimension_diagram") and data.get("length") and data.get("width") and data.get("height"):
                result = {
                    "Assembled Length (in.)": str(data["length"]),
                    "Assembled Width (in.)": str(data["width"]),
                    "Assembled Height (in.)": str(data["height"]),
                    "_vl_notes": data.get("notes") or "",
                    "_vl_image": url,
                }
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
                print(
                    f"    VL dims from image#{idx}: {result['Assembled Length (in.)']}x"
                    f"{result['Assembled Width (in.)']}x{result['Assembled Height (in.)']}",
                    flush=True,
                )
                return result
        except Exception as exc:
            print(f"    VL skip image#{idx}: {exc}", flush=True)
            continue
    return {}


def apply_excel_overrides(scraped: dict, row: dict, package_from_children: dict, vl_dims: dict) -> dict:
    attrs: dict = {}
    specs: dict = {}
    dims = dict(scraped.get("dims") or {})
    dims.update({k: v for k, v in vl_dims.items() if not str(k).startswith("_")})
    for k in ("Assembled Length (in.)", "Assembled Width (in.)", "Assembled Height (in.)"):
        if dims.get(k):
            attrs[k] = str(dims[k])
    net = _num(row.get("net_weight_lbs"))
    gross = _num(row.get("gross_weight_lbs"))
    if net:
        attrs["Product Weight (lbs.)"] = str(net)
    elif dims.get("Product Weight (lbs.)"):
        attrs["Product Weight (lbs.)"] = str(dims["Product Weight (lbs.)"])

    for k in ("Package Length (in.)", "Package Width (in.)", "Package Height (in.)"):
        if dims.get(k):
            specs[k] = str(dims[k])
        elif package_from_children.get(k):
            specs[k] = str(package_from_children[k])
    if gross:
        specs["Package Weight (lbs.)"] = str(gross)
    elif dims.get("Package Weight (lbs.)"):
        specs["Package Weight (lbs.)"] = str(dims["Package Weight (lbs.)"])

    box_count = int(float(row.get("combo_box_count") or 1))
    is_combo = str(row.get("is_combo")).lower() == "true" or box_count > 1
    if is_combo:
        specs["Combo Box Count"] = str(box_count)
        attrs["Product Type"] = "Combo Item"
        note = f"Ships as {box_count} separate carton(s); components must be connected before use."
        scraped["description"] = (scraped.get("description") or "") + f"<p>{note}</p>"
        if row.get("child_combo"):
            attrs["Combo Components"] = str(row["child_combo"])[:200]
        if vl_dims.get("_vl_notes"):
            scraped["description"] += f"<p>Overall layout: {vl_dims['_vl_notes']}</p>"

    return {
        "sku": scraped["sku"],
        "title": scraped["title"],
        "price": _num(row.get("table_discount_price")) or 0.0,
        "shipping": _num(row.get("table_shipping")) or 0.0,
        "stock": int(float(row.get("stock") or 0)),
        "description": scraped.get("description") or "",
        "images": scraped.get("images") or [],
        "videos": [],
        "attributes": attrs,
        "specs": specs,
        "url": scraped.get("url") or "",
        "_meta": {
            "dims_from_page": scraped.get("dims") or {},
            "vl_dims": {k: v for k, v in vl_dims.items() if not str(k).startswith("_")},
            "package_from_children": package_from_children,
            "price_source": "excel_单买折扣价_page_exclusive",
            "shipping_source": row.get("shipping_source") or "excel",
            "missing_assembled": not all(
                attrs.get(k)
                for k in ("Assembled Length (in.)", "Assembled Width (in.)", "Assembled Height (in.)")
            ),
            "missing_package_lwh": not all(
                specs.get(k)
                for k in ("Package Length (in.)", "Package Width (in.)", "Package Height (in.)")
            ),
        },
    }


def upsert_db(payload: dict, meta: dict) -> None:
    db = SessionLocal()
    try:
        sku = payload["sku"]
        row = db.query(CollectedProduct).filter_by(sku=sku).first()
        logs = [
            "w714_page_collect",
            f"price_source={meta.get('price_source')}",
            f"shipping_source={meta.get('shipping_source')}",
        ]
        if meta.get("vl_dims"):
            logs.append("assembled_from_dimension_image_vl")
        if meta.get("package_from_children"):
            logs.append("package_lwh_from_child_cartons")
        if row:
            row.title = payload["title"]
            row.description = payload["description"]
            row.attributes = payload["attributes"]
            row.specs = payload["specs"]
            row.images = payload["images"]
            row.videos = payload["videos"]
            row.price = payload["price"]
            row.shipping = payload["shipping"]
            row.stock = payload["stock"]
            row.url = payload["url"]
            row.status = "COLLECTED"
            row.optimization = None
            row.logs = (row.logs or []) + logs
            for f in ("attributes", "specs", "images", "videos", "logs"):
                flag_modified(row, f)
        else:
            db.add(
                CollectedProduct(
                    sku=sku,
                    title=payload["title"],
                    price=payload["price"],
                    shipping=payload["shipping"],
                    stock=payload["stock"],
                    description=payload["description"],
                    images=payload["images"],
                    videos=payload["videos"],
                    attributes=payload["attributes"],
                    specs=payload["specs"],
                    url=payload["url"],
                    status="COLLECTED",
                    logs=logs,
                )
            )
        db.commit()
    finally:
        db.close()


def update_measure_pack(batch: str, results: list[dict]) -> None:
    path = W714_DIR / f"{batch}_measure_pack.csv"
    if not path.exists():
        return
    by_sku = {r["sku"]: r for r in results}
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    fields = list(rows[0].keys()) if rows else []
    out = []
    for row in rows:
        hit = by_sku.get(row["sku"])
        if hit:
            meta = hit.get("_meta") or {}
            attrs = hit.get("attributes") or {}
            specs = hit.get("specs") or {}
            row["assembled_length_in"] = attrs.get("Assembled Length (in.)", "")
            row["assembled_width_in"] = attrs.get("Assembled Width (in.)", "")
            row["assembled_height_in"] = attrs.get("Assembled Height (in.)", "")
            row["package_length_in"] = specs.get("Package Length (in.)", "")
            row["package_width_in"] = specs.get("Package Width (in.)", "")
            row["package_height_in"] = specs.get("Package Height (in.)", "")
            row["page_price"] = hit.get("price", "")
            row["page_shipping"] = hit.get("shipping", "")
            row["status"] = "hold_missing_dims" if meta.get("missing_assembled") else "collected"
            notes = []
            if meta.get("vl_dims"):
                notes.append("assembled_from_vl")
            if meta.get("package_from_children"):
                notes.append("package_from_children")
            if meta.get("missing_assembled"):
                notes.append("missing_assembled")
            if meta.get("missing_package_lwh"):
                notes.append("missing_package_lwh")
            row["notes"] = ",".join(notes)
        out.append(row)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)


def existing_skus(status_ok: set[str] | None = None) -> set[str]:
    db = SessionLocal()
    try:
        q = db.query(CollectedProduct.sku, CollectedProduct.status, CollectedProduct.attributes)
        out = set()
        for sku, status, attrs in q:
            if status_ok and status not in status_ok:
                continue
            attrs = attrs or {}
            if attrs.get("Assembled Length (in.)") and attrs.get("Assembled Width (in.)") and attrs.get(
                "Assembled Height (in.)"
            ):
                out.add(sku)
        return out
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default=None)
    ap.add_argument("--sku-list", type=Path, default=None)
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--no-vl", action="store_true")
    args = ap.parse_args()
    rows = load_candidates(args.batch, args.sku_list)
    if args.skip_existing:
        have = existing_skus()
        rows = [r for r in rows if r["sku"] not in have]
    if not rows:
        print("no candidates")
        return 0

    client = None
    if not args.no_vl and os.getenv("QWEN_API_KEY"):
        client = OpenAI(
            api_key=os.getenv("QWEN_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    print(f"collecting {len(rows)} skus batch={args.batch}")
    results = []
    holds = []
    child_cache: dict = {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        for i, row in enumerate(rows, 1):
            sku = row["sku"]
            try:
                scraped = scrape_product_page(page, sku)
                vl_dims = {}
                page_has_asm = all(
                    (scraped.get("dims") or {}).get(k)
                    for k in (
                        "Assembled Length (in.)",
                        "Assembled Width (in.)",
                        "Assembled Height (in.)",
                    )
                )
                if not page_has_asm:
                    vl_dims = vl_assembled_from_images(sku, scraped.get("images") or [], client)

                package_from_children = {}
                page_has_pkg = all(
                    (scraped.get("dims") or {}).get(k)
                    for k in (
                        "Package Length (in.)",
                        "Package Width (in.)",
                        "Package Height (in.)",
                    )
                )
                children = child_skus_from_combo(row.get("child_combo") or "")
                if not page_has_pkg and children:
                    package_from_children = package_dims_from_children(page, children, child_cache)

                payload = apply_excel_overrides(scraped, row, package_from_children, vl_dims)
                meta = payload["_meta"]
                upsert_db({k: v for k, v in payload.items() if k != "_meta"}, meta)
                if meta.get("missing_assembled"):
                    holds.append(sku)
                    status = "HOLD"
                else:
                    status = "OK"
                print(
                    f"  [{i}/{len(rows)}] {sku} {status} price={payload['price']} "
                    f"ship={payload['shipping']} imgs={len(payload['images'])} "
                    f"asm={payload['attributes'].get('Assembled Length (in.)')}x"
                    f"{payload['attributes'].get('Assembled Width (in.)')}x"
                    f"{payload['attributes'].get('Assembled Height (in.)')} "
                    f"pkg={payload['specs'].get('Package Length (in.)')} "
                    f"net={payload['attributes'].get('Product Weight (lbs.)')} "
                    f"gross={payload['specs'].get('Package Weight (lbs.)')}"
                )
                results.append(payload)
            except Exception as exc:
                print(f"  [{i}/{len(rows)}] {sku} FAIL {exc}")
                holds.append(sku)
            time.sleep(1.0)
        browser.close()

    if args.batch:
        update_measure_pack(args.batch, results)
        report = {
            "batch": args.batch,
            "ok": [r["sku"] for r in results if not (r.get("_meta") or {}).get("missing_assembled")],
            "hold_missing_dims": holds,
            "count_ok": len([r for r in results if not (r.get("_meta") or {}).get("missing_assembled")]),
            "count_hold": len(holds),
        }
        (W714_DIR / f"{args.batch}_collect_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("report", report)
    print(f"done results={len(results)} holds={len(holds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
