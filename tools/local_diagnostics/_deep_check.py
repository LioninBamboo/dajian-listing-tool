from __future__ import annotations

"""Deep-inspect description content for SKUs with weight/size confusion."""

import json
import re
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

SKUS = ["SP110142AAE", "SP110142AAA", "SP100031AAA", "SP100015AAA", "SP000046AAE"]


def extract_measures(text: str) -> list[tuple[float, str, str, str]]:
    results = []
    text_clean = re.sub(r"<[^>]+>", " ", text)
    text_clean = re.sub(r"\s+", " ", text_clean)
    pattern = r"([\d]+\.?[\d]*)\s*(lbs?\.?|pounds?|in\.?|inch(?:es)?|ft\.?|feet|mm|cm|m|kg)\b"
    for match in re.finditer(pattern, text_clean):
        value = match.group(1)
        unit = match.group(2)
        start = max(0, match.start() - 60)
        end = min(len(text_clean), match.end() + 60)
        context = text_clean[start:end].strip()
        results.append((float(value), unit, context, f"{value} {unit}"))
    return results


def main() -> None:
    conn = sqlite3.connect(ROOT / "ebay_collection.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    for sku in SKUS:
        cur.execute(
            "SELECT sku, listing_id, title, attributes, specs, description, optimization "
            "FROM collected_products WHERE sku = ?",
            (sku,),
        )
        row = cur.fetchone()
        if not row:
            print(f"SKU {sku} NOT FOUND in DB!")
            continue

        db_desc = row["description"] or ""
        db_attrs = json.loads(row["attributes"]) if isinstance(row["attributes"], str) else (row["attributes"] or {})
        db_specs = json.loads(row["specs"]) if isinstance(row["specs"], str) else (row["specs"] or {})
        opt = json.loads(row["optimization"]) if isinstance(row["optimization"], str) else (row["optimization"] or {})
        opt_desc = opt.get("description", "")

        print("=" * 80)
        print(f"SKU: {sku}")
        print(f"eBay: https://www.ebay.com/itm/{row['listing_id']}")
        print(f"DB title: {row['title']}")
        print("=" * 80)

        print()
        print("--- Gigacloud source description entries ---")
        gc_measures = {}
        if db_desc:
            for value, unit, context, raw in extract_measures(db_desc):
                gc_measures[(round(value, 2), unit)] = (raw, context[:100])
                print(f"  {raw}  -> {context[:100]}")

        print()
        print("--- eBay listing description entries (from opt) ---")
        ebay_measures = {}
        if opt_desc:
            for value, unit, context, raw in extract_measures(opt_desc):
                ebay_measures[(round(value, 2), unit)] = (raw, context[:100])
                print(f"  {raw}  -> {context[:100]}")

        print()
        print("--- DISCREPANCIES ---")
        discrepancies_found = False
        all_keys = set(gc_measures) | set(ebay_measures)
        for key in sorted(all_keys):
            in_gc = key in gc_measures
            in_ebay = key in ebay_measures
            if in_gc and in_ebay:
                continue
            if in_gc and not in_ebay:
                raw, context = gc_measures[key]
                print(f"  MISSING on eBay: {raw} (source context: {context[:80]})")
                discrepancies_found = True
            elif in_ebay and not in_gc:
                raw, context = ebay_measures[key]
                print(f"  EXTRA on eBay: {raw} (context: {context[:80]})")
                discrepancies_found = True

        gc_by_unit = {}
        ebay_by_unit = {}
        for (value, unit), (raw, context) in gc_measures.items():
            gc_by_unit.setdefault(unit, []).append((value, raw, context))
        for (value, unit), (raw, context) in ebay_measures.items():
            ebay_by_unit.setdefault(unit, []).append((value, raw, context))

        print()
        print("--- Cross-reference by unit ---")
        for unit in sorted(set(gc_by_unit) | set(ebay_by_unit)):
            gc_vals = sorted(gc_by_unit.get(unit, []), key=lambda item: item[0])
            ebay_vals = sorted(ebay_by_unit.get(unit, []), key=lambda item: item[0])
            print(f"  Unit: {unit}")
            print(f"    Source (大建): {[f'{v}({ctx[:30]})' for v, _, ctx in gc_vals]}")
            print(f"    eBay:          {[f'{v}({ctx[:30]})' for v, _, ctx in ebay_vals]}")
            gc_only = set(value for value, _, _ in gc_vals)
            ebay_only = set(value for value, _, _ in ebay_vals)
            if gc_only != ebay_only:
                missing = gc_only - ebay_only
                extra = ebay_only - gc_only
                if missing:
                    print(f"    ! Values in source but NOT on eBay: {sorted(missing)}")
                if extra:
                    print(f"    ! Values on eBay but NOT in source: {sorted(extra)}")
                print()

        print()
        print("--- DB Attributes & Specs (reference) ---")
        for key, value in sorted(db_attrs.items()):
            if any(token in key.lower() for token in ["weight", "length", "width", "height", "lbs", "inch", "size"]):
                print(f"  Attr: {key} = {value}")
        for key, value in sorted(db_specs.items()):
            if any(token in key.lower() for token in ["weight", "length", "width", "height", "lbs", "inch", "size", "package"]):
                print(f"  Spec: {key} = {value}")

        if not discrepancies_found:
            print("  (no significant discrepancies found)")

        print()
        print()

    conn.close()


if __name__ == "__main__":
    main()

