"""Backfill market keywords into existing listings' stored optimization.

The Browse market-intel call 403'd for months (it used the sub-account USER
token instead of the APPLICATION token), so every listing generated in that
window has an EMPTY ``top_keywords`` — which is why the daily cro_title_rewrite
task finds "no safe keywords to add" and never optimizes a title. Now that
Browse works (app token), re-fetch real market keywords for existing listings
and store them, so the rewrite task has real search terms to enrich with.

Idempotent: skips a SKU that already has non-empty top_keywords unless --force.
Rate-limited. Dry-run by default; --apply writes to the local DB (no eBay write).

  python scripts/backfill_market_keywords.py --limit 50            # preview
  python scripts/backfill_market_keywords.py --limit 50 --apply    # write to DB
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB = PROJECT_ROOT / "ebay_collection.db"


def _has_keywords(opt: dict) -> bool:
    if opt.get("top_keywords"):
        return True
    mi = opt.get("market_intel")
    return bool(isinstance(mi, dict) and mi.get("top_keywords"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill market keywords into stored optimizations")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--statuses", default="PUBLISHED,READY,READY_TO_PUBLISH")
    ap.add_argument("--apply", action="store_true", help="write to the local DB (default dry-run)")
    ap.add_argument("--force", action="store_true", help="refresh even SKUs that already have keywords")
    ap.add_argument("--sleep", type=float, default=0.6)
    args = ap.parse_args()

    if not os.getenv("QWEN_API_KEY"):
        os.environ["QWEN_API_KEY"] = "backfill-noop"  # QwenOptimizer init needs a key; we only call Browse
    from qwen_optimizer import QwenOptimizer
    q = object.__new__(QwenOptimizer)

    statuses = tuple(s.strip().upper() for s in args.statuses.split(",") if s.strip())
    conn = sqlite3.connect(args.db)
    placeholders = ",".join("?" * len(statuses))
    rows = list(conn.execute(
        f"SELECT sku, optimization FROM collected_products WHERE upper(status) IN ({placeholders})",
        statuses,
    ))

    updated = skipped = failed = 0
    for sku, opt_raw in rows:
        if updated >= args.limit:
            break
        try:
            opt = json.loads(opt_raw or "{}")
        except Exception:
            continue
        if _has_keywords(opt) and not args.force:
            skipped += 1
            continue
        title = opt.get("title") or ""
        if not title:
            skipped += 1
            continue
        try:
            mi = QwenOptimizer.fetch_market_intelligence(q, title, opt.get("categoryId"))
        except Exception as e:
            print(f"  [FAIL] {sku}: {e}")
            failed += 1
            continue
        kws = mi.get("top_keywords") or []
        if not kws:
            print(f"  [NONE] {sku}: no keywords returned (total={mi.get('total_listings')})")
            skipped += 1
            time.sleep(args.sleep)
            continue
        opt["top_keywords"] = kws[:20]
        opt["market_intel"] = {
            "top_keywords": kws[:20],
            "price_stats": mi.get("price_stats") or {},
            "total_listings": mi.get("total_listings", 0),
        }
        print(f"  [{'WRITE' if args.apply else 'DRY'}] {sku}: {len(kws)} kw -> {kws[:8]}")
        if args.apply:
            conn.execute("UPDATE collected_products SET optimization=? WHERE sku=?",
                         (json.dumps(opt), sku))
            conn.commit()
        updated += 1
        time.sleep(args.sleep)

    print(f"\nDone. updated={updated} skipped={skipped} failed={failed} "
          f"(mode={'APPLY' if args.apply else 'DRY-RUN'})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
