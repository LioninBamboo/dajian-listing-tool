#!/usr/bin/env python3
"""Export published modular/sectional SKUs still marked Assembly Required=No."""
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "ebay_collection.db"
DEFAULT_OUT = ROOT / "logs" / "modular_sectional_assembly_skus.txt"

PATTERN = re.compile(
    r"\b(modular|sectional|sofa set|matching ottoman|individual ottoman)\b",
    re.IGNORECASE,
)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    con = sqlite3.connect(DB)
    skus: list[str] = []
    for sku, title, optimization in con.execute(
        "SELECT sku, title, optimization FROM collected_products WHERE status = 'PUBLISHED'"
    ):
        opt = json.loads(optimization or "{}")
        aspects = opt.get("aspects") or {}
        assembly = aspects.get("Assembly Required")
        if isinstance(assembly, list):
            assembly = assembly[0] if assembly else ""
        text = f"{title} {opt.get('title', '')}"
        if not PATTERN.search(text):
            continue
        if str(assembly).strip().lower() != "no":
            continue
        skus.append(sku)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(skus) + ("\n" if skus else ""), encoding="utf-8")
    print(f"written {len(skus)} SKUs to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
