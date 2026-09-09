#!/usr/bin/env python3
"""Regenerate each READY-unpublished garden product until the publish-time semantic
FactSheet guard passes, then overwrite its stored optimization. batch_analyze only
runs the deterministic gate; the semantic guard (Qwen claim-diff) is what blocks at
publish, so pre-clear it here to avoid publish-time failures.

    python scripts/pilot_harden_garden.py --skus-file pilot_skus.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from qwen_optimizer import QwenOptimizer
from src.utils.listing_fact_sheet import check_fact_sheet_violations


def _loads(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus-file", required=True)
    ap.add_argument("--retries", type=int, default=6)
    args = ap.parse_args()
    skus = json.load(open(args.skus_file, encoding="utf-8"))

    conn = sqlite3.connect(str(ROOT / "ebay_collection.db"))
    conn.row_factory = sqlite3.Row
    opt = QwenOptimizer(api_key=os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"))

    ok = blocked = 0
    for sku in skus:
        row = conn.execute("SELECT * FROM collected_products WHERE sku=?", (sku,)).fetchone()
        if not row:
            print(f"{sku}: not found"); continue
        d = dict(row)
        attrs, specs = _loads(d.get("attributes")), _loads(d.get("specs"))
        opt_json = _loads(d.get("optimization"))
        mi = None
        kws = (opt_json.get("top_keywords") if isinstance(opt_json, dict) else None)
        if kws:
            mi = {"top_keywords": kws}

        chosen = None
        for attempt in range(1, args.retries + 1):
            r = opt.optimize_garden_lifestyle_listing(
                d.get("title") or "", d.get("description") or "",
                attributes=attrs, specs=specs, market_intel=mi,
            )
            if r.get("error"):
                print(f"  {sku} attempt {attempt}: gen FAIL"); continue
            fc = sqlite3.connect(str(ROOT / "fact_sheet_cache.db"))
            v = check_fact_sheet_violations(
                fc, d.get("title") or "", d.get("description") or "", attrs, specs,
                r.get("title", ""), r.get("description", ""), r.get("aspects") or {},
            )
            fc.close()
            status = (v or {}).get("status")
            viols = (v or {}).get("violations") or []
            print(f"  {sku} attempt {attempt}: guard={status}/{len(viols)}"
                  + (f"  {[x.get('claim_text') for x in viols[:3]]}" if viols else ""))
            if status != "violations":
                chosen = r
                break
        if not chosen:
            blocked += 1
            print(f"{sku}: STILL BLOCKED after {args.retries} — leaving as-is (batch_publish will skip)")
            continue
        # merge the clean copy into the stored optimization
        if not isinstance(opt_json, dict):
            opt_json = {}
        opt_json["title"] = chosen.get("title", "")
        opt_json["description"] = chosen.get("description", "")
        if chosen.get("aspects"):
            opt_json["aspects"] = chosen["aspects"]
        conn.execute("UPDATE collected_products SET optimization=? WHERE sku=?",
                     (json.dumps(opt_json, ensure_ascii=False), sku))
        conn.commit()
        ok += 1
        print(f"{sku}: CLEARED ✓")
    conn.close()
    print(f"\nhardened: {ok} cleared, {blocked} still blocked")


if __name__ == "__main__":
    main()
