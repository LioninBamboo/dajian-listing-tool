#!/usr/bin/env python3
"""Backfill and audit eBay Motors compatibility metadata in the local database."""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.vehicle_compatibility import (
    EBAY_MOTORS_CATEGORIES,
    analyze_ebay_motors_compatibility,
    apply_compatibility_aspects,
    serialize_compatibility_analysis,
)
from src.utils.store_profile import get_store_profile

DB_PATH = ROOT / "ebay_collection.db"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def parse_json(value, default):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def load_rows(conn, sku_filter=None):
    profile = get_store_profile()
    base_sql = (
        "SELECT sku, status, title, description, optimization, listing_id "
        "FROM collected_products "
        "WHERE status IN ('READY', 'READY_TO_PUBLISH', 'PUBLISHED') "
        "ORDER BY status, sku"
    )
    rows = conn.execute(base_sql).fetchall()
    results = []
    for row in rows:
        opt = parse_json(row["optimization"], {})
        category_id = str(opt.get("categoryId", "") or "")
        if not profile.is_motors and category_id not in EBAY_MOTORS_CATEGORIES:
            continue
        if sku_filter and row["sku"] != sku_filter:
            continue
        results.append(row)
    return results


def main():
    parser = argparse.ArgumentParser(description="Build local eBay Motors compatibility metadata")
    parser.add_argument("--apply", action="store_true", help="Persist changes back to the database")
    parser.add_argument("--sku", help="Only process one SKU")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = load_rows(conn, args.sku)
    is_motors_store = get_store_profile().is_motors

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = LOG_DIR / f"motors_compatibility_{timestamp}.json"

    print("=" * 60)
    print(f"Motors Compatibility Backfill | rows={len(rows)} | mode={'APPLY' if args.apply else 'AUDIT'}")
    print("=" * 60)

    updated = 0
    report = []

    for row in rows:
        opt = parse_json(row["optimization"], {})
        title = opt.get("title") or row["title"] or ""
        description = opt.get("description") or row["description"] or ""
        category_id = str(opt.get("categoryId", "") or "")
        aspects = parse_json(opt.get("aspects", {}), {})

        analysis = analyze_ebay_motors_compatibility(
            category_id,
            title,
            description,
            aspects,
            is_motors_store=is_motors_store,
        )
        updated_aspects = apply_compatibility_aspects(category_id, aspects, analysis)
        changed = (
            updated_aspects != aspects or
            opt.get("motorsCompatibility", {}) != serialize_compatibility_analysis(analysis)
        )

        print(f"{row['sku']} | {row['status']} | {category_id} | {analysis.mode} | compat={len(analysis.compatible_products)}")
        if analysis.issues:
            for issue in analysis.issues:
                print(f"  - {issue}")

        if args.apply and changed:
            opt["aspects"] = updated_aspects
            opt["motorsCompatibility"] = serialize_compatibility_analysis(analysis)
            conn.execute(
                "UPDATE collected_products SET optimization = ?, updated_at = ? WHERE sku = ?",
                (json.dumps(opt, ensure_ascii=False), datetime.now(timezone.utc).isoformat(), row["sku"]),
            )
            updated += 1

        report.append({
            "sku": row["sku"],
            "status": row["status"],
            "listing_id": row["listing_id"],
            "category_id": category_id,
            "mode": analysis.mode,
            "compatibility_count": len(analysis.compatible_products),
            "summary": analysis.summary,
            "issues": analysis.issues,
            "changed": changed,
        })

    if args.apply:
        conn.commit()
    conn.close()

    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("-" * 60)
    print(f"Updated rows: {updated}")
    print(f"Report: {report_path}")
    print("-" * 60)


if __name__ == "__main__":
    main()
