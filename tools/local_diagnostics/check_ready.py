from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def parse_json(raw):
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def main() -> None:
    conn = sqlite3.connect(ROOT / "ebay_collection.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM collected_products WHERE status='READY'").fetchall()

    print(f"Found {len(rows)} READY products.")

    for row in rows:
        sku = row["sku"]
        optimization = parse_json(row["optimization"])
        attributes = parse_json(row["attributes"])
        videos = parse_json(row["videos"])

        print(f"\nSKU: {sku}")
        print(f"Title: {optimization.get('title') or row['title']}")
        print(f"Category: {optimization.get('categoryId')}")
        print(f"Videos: {len(videos)} found")
        print(f"Params count: {len(attributes)}")

    conn.close()


if __name__ == "__main__":
    main()

