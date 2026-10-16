from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "reports" / "compare_batch2_20260607_222828.json"

os.environ["PYTHONIOENCODING"] = "utf-8"


def main() -> None:
    data = json.loads(REPORT.read_text(encoding="utf-8"))

    print("HIGH severity products:")
    print("=" * 80)
    for row in data["results"]:
        if row["max_severity"] != "HIGH":
            continue
        print(row["ebay_url"])
        for discrepancy in row["discrepancies"]:
            if discrepancy["severity"] == "HIGH":
                print(f"  [{discrepancy['severity']}] {discrepancy['field']}")
                print(f"    issue: {discrepancy['issue']}")
        print()

    print()
    print("=" * 80)
    print("MEDIUM severity: Item Specifics/Description (skipping title-only)")
    print("=" * 80)
    for row in data["results"]:
        important = [
            discrepancy
            for discrepancy in row["discrepancies"]
            if discrepancy["severity"] == "MEDIUM"
            and ("Item Specifics" in discrepancy["field"] or "描述内容" in discrepancy["field"])
        ]
        if not important or row["max_severity"] == "HIGH":
            continue
        print(row["ebay_url"])
        for discrepancy in important:
            print(f"  [{discrepancy['severity']}] {discrepancy['field']}: {discrepancy['issue'][:120]}")
        print()


if __name__ == "__main__":
    main()

