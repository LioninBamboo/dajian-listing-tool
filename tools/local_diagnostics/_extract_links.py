from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "reports" / "compare_batch2_20260607_222828.json"


def main() -> None:
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    results = data["results"]

    print(f"=== HIGH 级偏差 ({len([row for row in results if row['max_severity'] == 'HIGH'])}个产品) ===")
    print()

    for row in results:
        if row["max_severity"] != "HIGH":
            continue
        print(row["ebay_url"])
        print(f"  DB: {row['db_title']}")
        print(f"  eBay: {row['ebay_title']}")
        for discrepancy in row["discrepancies"]:
            if discrepancy["severity"] == "HIGH":
                print(f"  [{discrepancy['severity']}] {discrepancy['field']}: {discrepancy['issue']}")
        print()

    print()
    print("=== MEDIUM 级重要偏差 (Item Specifics/描述内容) ===")
    print()

    for row in results:
        important = [
            discrepancy
            for discrepancy in row["discrepancies"]
            if discrepancy["severity"] == "MEDIUM"
            and ("Item Specifics" in discrepancy["field"] or "描述内容" in discrepancy["field"])
        ]
        if not important:
            continue
        print(row["ebay_url"])
        print(f"  DB: {row['db_title']}")
        for discrepancy in important:
            print(f"  [{discrepancy['severity']}] {discrepancy['field']}: {discrepancy['issue']}")
        print()


if __name__ == "__main__":
    main()

