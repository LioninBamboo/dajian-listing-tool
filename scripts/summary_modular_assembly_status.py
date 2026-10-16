#!/usr/bin/env python3
"""Summarize modular/sectional assembly status after batch fix."""
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "ebay_collection.db"
SKU_FILE = ROOT / "logs" / "modular_sectional_assembly_skus.txt"

PATTERN = re.compile(
    r"\b(modular|sectional|sofa set|matching ottoman|individual ottoman)\b",
    re.IGNORECASE,
)


def main() -> None:
    skus = [
        line.strip()
        for line in SKU_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    con = sqlite3.connect(DB)
    still_no = []
    now_yes = []
    for sku in skus:
        row = con.execute(
            "SELECT optimization FROM collected_products WHERE sku = ?",
            (sku,),
        ).fetchone()
        if not row:
            continue
        opt = json.loads(row[0] or "{}")
        assembly = (opt.get("aspects") or {}).get("Assembly Required")
        if isinstance(assembly, list):
            assembly = assembly[0] if assembly else ""
        val = str(assembly or "").strip().lower()
        if val == "no":
            still_no.append(sku)
        elif val == "yes":
            now_yes.append(sku)

    print(f"scope_skus={len(skus)}")
    print(f"assembly_yes={len(now_yes)}")
    print(f"assembly_no={len(still_no)}")
    if still_no:
        print("still_no_sample:", still_no[:20])
        if len(still_no) > 20:
            print(f"  ... and {len(still_no) - 20} more")


if __name__ == "__main__":
    main()
