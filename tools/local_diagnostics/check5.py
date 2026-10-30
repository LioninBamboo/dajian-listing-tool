from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKUS = ["SP110142AAE", "SP110142AAA", "SP100031AAA", "SP100015AAA", "SP000046AAE"]


def extract_measures(text: str) -> list[tuple[str, str, str]]:
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned)
    matches: list[tuple[str, str, str]] = []
    pattern = r"([\d]+\.?[\d]*)\s*(lbs?\.?|pounds?|in\.?|inch(?:es)?|ft\.?|feet|mm|cm|m|kg)\b"
    for match in re.finditer(pattern, cleaned):
        value = match.group(1)
        unit = match.group(2)
        start = max(0, match.start() - 60)
        end = min(len(cleaned), match.end() + 60)
        matches.append((value, unit, cleaned[start:end].strip()))
    return matches


def main() -> None:
    conn = sqlite3.connect(ROOT / "ebay_collection.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    for sku in SKUS:
        cur.execute(
            "SELECT sku, listing_id, title, attributes, description, optimization "
            "FROM collected_products WHERE sku = ?",
            (sku,),
        )
        row = cur.fetchone()
        if not row:
            continue

        description = row["description"] or ""
        optimization = (
            json.loads(row["optimization"])
            if isinstance(row["optimization"], str)
            else (row["optimization"] or {})
        )
        optimized_description = optimization.get("description", "")
        attributes = (
            json.loads(row["attributes"])
            if isinstance(row["attributes"], str)
            else (row["attributes"] or {})
        )

        print("=" * 80)
        print(f"SKU: {sku}")
        print(f"LINK: https://www.ebay.com/itm/{row['listing_id']}")
        print(f"TITLE: {row['title']}")
        print("=" * 80)

        print("SOURCE desc values:")
        for value, unit, context in extract_measures(description):
            print(f"  {value} {unit} -> {context[:100]}")

        print()
        print("EBAY desc values:")
        for value, unit, context in extract_measures(optimized_description):
            print(f"  {value} {unit} -> {context[:100]}")

        source_values = {(round(float(value), 2), unit) for value, unit, _ in extract_measures(description)}
        ebay_values = {(round(float(value), 2), unit) for value, unit, _ in extract_measures(optimized_description)}

        print()
        print("DIFF:")
        only_in_source = source_values - ebay_values
        only_in_ebay = ebay_values - source_values
        if only_in_source:
            print(f"  In source but NOT on eBay: {sorted(only_in_source)}")
        if only_in_ebay:
            print(f"  On eBay but NOT in source: {sorted(only_in_ebay)}")
        if not only_in_source and not only_in_ebay:
            print("  All values match!")

        print()
        print("ATTRS (dimension-related):")
        for key, value in sorted(attributes.items()):
            lowered = key.lower()
            if any(word in lowered for word in ["weight", "length", "width", "height", "lbs", "inch", "size"]):
                print(f"  {key} = {value}")

        print()
        print()

    conn.close()


if __name__ == "__main__":
    main()
