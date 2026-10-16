#!/usr/bin/env python3
"""Regenerate-until-clean publisher.

The publish-time semantic FactSheet guard is a Qwen call — it's stochastic, so a
listing that failed on an unsourced-claim reach ("heavy-duty", "adjustable lift
height") often passes on a FRESH generation. But batch_publish retries the SAME
stored copy 3× and gives up. This driver loops at the batch level:

  round: publish READY  ->  classify failures  ->  regenerate the guard-blocked
  ones (reset to COLLECTED + batch_analyze)  ->  republish, up to --max-rounds.

Out-of-stock / dimension / category failures are NOT regenerated (they won't
change on a re-roll) — only semantic-guard blocks, which are the stochastic ones.

    python scripts/publish_with_regenerate.py --skus-file batch.json --max-rounds 4
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
DB = str(ROOT / "ebay_collection.db")
GUARD_MARKERS = ("factsheet", "semantic_", "unsupported claim", "fact violation")
SKIP_MARKERS = ("zero supplier", "out of stock", "has ended", "cannot determine ebay category",
                "measurement aspect", "brandmpn")


def _ready_no_id(skus):
    c = sqlite3.connect(DB)
    q = ("SELECT sku FROM collected_products WHERE status='READY' "
         "AND (listing_id IS NULL OR listing_id='') AND sku IN (%s)" % ",".join("?" * len(skus)))
    r = [x[0] for x in c.execute(q, skus).fetchall()]
    c.close()
    return r


def _latest_results():
    files = sorted(glob.glob(str(ROOT / "logs" / "publish_results_*.json")))
    if not files:
        return []
    try:
        return json.load(open(files[-1], encoding="utf-8"))
    except Exception:
        return []


def _classify(results):
    """Return (published, guard_failed, skipped) sku lists from a results array."""
    published, guard, skipped = [], [], []
    for r in results:
        sku = r.get("sku")
        if not sku:
            continue
        st = r.get("status")
        msg = str(r.get("message") or r.get("reason") or "").lower()
        if st == "success":
            published.append(sku)
        elif any(m in msg for m in GUARD_MARKERS) and not any(m in msg for m in SKIP_MARKERS):
            guard.append(sku)
        else:
            skipped.append(sku)
    return published, guard, skipped


def _run(cmd):
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=3600)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus-file", required=True)
    ap.add_argument("--max-rounds", type=int, default=4)
    args = ap.parse_args()
    all_skus = json.load(open(args.skus_file, encoding="utf-8"))

    total_pub, dropped = [], set()
    for rnd in range(1, args.max_rounds + 1):
        ready = [s for s in _ready_no_id(all_skus) if s not in dropped]
        if not ready:
            print(f"[round {rnd}] no READY left — done"); break
        print(f"[round {rnd}] publishing {len(ready)} READY ...", flush=True)
        _run([PY, str(ROOT / "batch_publish.py"), "--sku-list", ",".join(ready)])
        pub, guard, skip = _classify(_latest_results())
        total_pub += pub
        dropped |= set(skip)
        print(f"[round {rnd}] published={len(pub)} guard-blocked={len(guard)} "
              f"skipped(unrecoverable)={len(skip)}", flush=True)
        if not guard:
            print("[done] no stochastic guard-blocks remain"); break
        if rnd == args.max_rounds:
            print(f"[done] hit max rounds; {len(guard)} still guard-blocked"); break
        # Regenerate the guard-blocked ones: reset to COLLECTED, re-analyze (fresh copy).
        c = sqlite3.connect(DB)
        c.execute("UPDATE collected_products SET status='COLLECTED' WHERE sku IN (%s)"
                  % ",".join("?" * len(guard)), guard)
        c.commit(); c.close()
        print(f"[round {rnd}] regenerating {len(guard)} guard-blocked via batch_analyze ...", flush=True)
        _run([PY, str(ROOT / "batch_analyze.py")])

    print(f"\n=== TOTAL published this run: {len(set(total_pub))} | "
          f"unrecoverable: {len(dropped)} ===")


if __name__ == "__main__":
    main()
