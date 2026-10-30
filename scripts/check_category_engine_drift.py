#!/usr/bin/env python
"""Report where the two category engines disagree about the live catalogue.

Publishing asks ``EbayCategoryMatcher.canonicalize_category``; the live audit
asks ``listing_quality_gate.classify_listing_profile``. They are separate
keyword engines answering the same question, and neither is strictly better —
the profile generalises a patio dining table to "Tables", the matcher
generalises an indoor storage bench to whatever its ladder hits first. So they
are not merged.

What must not happen is silent divergence: an item published into the matcher's
category that the audit then flags forever as belonging in the profile's. That
was 23 of 871 PUBLISHED SKUs when first measured (2026-07-28).

This script makes the divergence visible and countable. Run it after touching
either engine.

    python scripts/check_category_engine_drift.py                 # summary
    python scripts/check_category_engine_drift.py --list          # every case
    python scripts/check_category_engine_drift.py --max 23        # exit 1 above N
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "ebay_collection.db"


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def collect_disagreements(conn) -> list[dict]:
    from src.services.ebay_category_matcher import EbayCategoryMatcher
    from src.utils.listing_quality_gate import classify_listing_profile

    class _Oauth:
        def get_application_token(self):
            return "offline"

    matcher = EbayCategoryMatcher(_Oauth())
    rows: list[dict] = []
    for sku, title, optimization in conn.execute(
        "SELECT sku, title, optimization FROM collected_products WHERE status = 'PUBLISHED'"
    ):
        try:
            opt = json.loads(optimization or "{}")
        except (TypeError, ValueError):
            continue
        stored = str(opt.get("categoryId", "") or "").strip()
        context = " ".join(p for p in (title, opt.get("title", "")) if p).strip()
        description = _plain(opt.get("description") or "")

        profile = classify_listing_profile(context, description, stored)
        matched, _name = matcher.canonicalize_category(
            context, stored, opt.get("categoryName"), description
        )
        profile_verdict = profile.category_id or None
        matcher_verdict = matched if matched and matched != stored else None
        if profile_verdict and matcher_verdict and profile_verdict != matcher_verdict:
            rows.append(
                {
                    "sku": sku,
                    "stored": stored,
                    "profile": profile_verdict,
                    "matcher": matcher_verdict,
                    "kind": profile.kind,
                    "title": context[:70],
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print every disagreement")
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="exit 1 when disagreements exceed this count (regression gate)",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"[ERROR] database not found: {DB_PATH}")
        return 2

    conn = sqlite3.connect(DB_PATH)
    try:
        rows = collect_disagreements(conn)
        total = conn.execute(
            "SELECT COUNT(*) FROM collected_products WHERE status = 'PUBLISHED'"
        ).fetchone()[0]
    finally:
        conn.close()

    print(f"PUBLISHED SKUs      : {total}")
    print(f"engine disagreements: {len(rows)}")
    if args.list and rows:
        print()
        print(f"{'SKU':20} {'stored':8} {'profile':8} {'matcher':8} title")
        for row in rows:
            print(
                f"{row['sku']:20} {row['stored']:8} {row['profile']:8} "
                f"{row['matcher']:8} {row['title']}"
            )
    if args.max is not None and len(rows) > args.max:
        print(f"\nRESULT: FAIL — {len(rows)} disagreements exceeds --max {args.max}")
        return 1
    print("\nRESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
