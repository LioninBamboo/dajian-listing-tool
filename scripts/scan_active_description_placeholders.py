#!/usr/bin/env python3
"""Scan published listings for exact description measurement placeholder rows.

This is a lightweight alternative to ad-hoc Pylance snippets for large scans.
It supports a fast DB-only pass and an optional live eBay pass, then writes a
JSON report that can be resumed or consumed by existing repair scripts.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name)
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
    elif getattr(_stream, "buffer", None) is not None:
        setattr(
            sys,
            _stream_name,
            io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace", line_buffering=True),
        )

from src.utils.dimension_helpers import extract_all_dimensions, extract_product_weight_from_text


DB_PATH = ROOT / "ebay_collection.db"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

DIMENSION_ROW_PATTERN = re.compile(
    r'<tr[^>]*>\s*<td[^>]*>\s*Overall\s+Dimensions\s*\(L.?W.?H\)\s*</td>\s*<td[^>]*>\s*(.*?)\s*</td>\s*</tr>',
    re.IGNORECASE | re.DOTALL,
)
WEIGHT_ROW_PATTERN = re.compile(
    r'<tr[^>]*>\s*<td[^>]*>\s*((?:(?:Overall|Item|Product)\s+)?Weight(?:\s*\([^)]*\))?)\s*</td>\s*<td[^>]*>\s*(.*?)\s*</td>\s*</tr>',
    re.IGNORECASE | re.DOTALL,
)
PLACEHOLDER_PREFIXES = (
    "not specified",
    "not available",
    "n/a",
    "na",
    "see description",
)


def parse_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def expand_sku_filter(raw_values: Optional[List[str]]) -> List[str]:
    expanded: List[str] = []
    for raw in raw_values or []:
        for part in raw.split(","):
            sku = part.strip()
            if sku and sku not in expanded:
                expanded.append(sku)
    return expanded


def merge_sku_filters(*groups: Iterable[str]) -> List[str]:
    merged: List[str] = []
    for group in groups:
        for sku in group or []:
            if sku and sku not in merged:
                merged.append(sku)
    return merged


def load_skus_from_report(report_path: str) -> List[str]:
    payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    matches = payload.get("matches") or []
    if not isinstance(matches, list):
        return []

    skus: List[str] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        sku = str(match.get("sku") or "").strip()
        if sku and sku not in skus:
            skus.append(sku)
    return skus


def should_use_empty_filter(input_report: Optional[str], sku_filter: List[str]) -> bool:
    return bool(input_report) and not sku_filter


def normalize_html_text(value: str) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", str(value), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_placeholder_value(value: str) -> bool:
    normalized = normalize_html_text(value).lower()
    return any(normalized.startswith(prefix) for prefix in PLACEHOLDER_PREFIXES)


def find_description_placeholders(description: str) -> Dict[str, List[Dict[str, str]]]:
    matches = {"dimensions": [], "weight": []}
    if not description:
        return matches

    for match in DIMENSION_ROW_PATTERN.finditer(description):
        value = normalize_html_text(match.group(1))
        if is_placeholder_value(value):
            matches["dimensions"].append({
                "label": "Overall Dimensions (L×W×H)",
                "value": value,
            })

    for match in WEIGHT_ROW_PATTERN.finditer(description):
        label = normalize_html_text(match.group(1))
        value = normalize_html_text(match.group(2))
        if is_placeholder_value(value):
            matches["weight"].append({"label": label, "value": value})

    return matches


def extract_source_measurements(attrs_raw, specs_raw) -> Dict[str, Optional[float]]:
    attrs = parse_json(attrs_raw)
    specs = parse_json(specs_raw)
    merged = {}
    merged.update(specs)
    merged.update(attrs)
    dims = extract_all_dimensions(merged)

    if dims.get("weight") is None:
        source_blob = json.dumps(attrs, ensure_ascii=False) + "\n" + json.dumps(specs, ensure_ascii=False)
        dims["weight"] = extract_product_weight_from_text(source_blob)

    return dims


def build_row_query(sku_filter: Optional[List[str]], limit: Optional[int]) -> tuple[str, tuple]:
    base_query = (
        "SELECT sku, title, attributes, specs, optimization, description "
        "FROM collected_products WHERE status = 'PUBLISHED'"
    )
    params: List[str] = []
    if sku_filter:
        placeholders = ",".join("?" for _ in sku_filter)
        base_query += f" AND sku IN ({placeholders})"
        params.extend(sku_filter)
    base_query += " ORDER BY updated_at DESC"
    if limit is not None:
        base_query += " LIMIT ?"
        params.append(limit)
    return base_query, tuple(params)


def load_published_rows(conn: sqlite3.Connection, sku_filter: Optional[List[str]], limit: Optional[int]) -> List[sqlite3.Row]:
    query, params = build_row_query(sku_filter, limit)
    return conn.execute(query, params).fetchall()


def get_description_for_source(row: sqlite3.Row, source: str, ebay_client=None) -> str:
    if source == "db":
        opt = parse_json(row["optimization"])
        return str(opt.get("description") or row["description"] or "")

    inventory_item = ebay_client.get_inventory_item(row["sku"]) or {}
    return str(((inventory_item.get("product") or {}).get("description")) or "")


def iter_issue_results(rows: Iterable[sqlite3.Row], source: str, issue: str, ebay_client=None) -> List[Dict[str, object]]:
    results: List[Dict[str, object]] = []
    issue_keys = ["dimensions", "weight"] if issue == "both" else [issue]

    for row in rows:
        source_dims = extract_source_measurements(row["attributes"], row["specs"])
        requested_measurements = {
            "dimensions": all(source_dims.get(key) is not None for key in ("length", "width", "height")),
            "weight": source_dims.get("weight") is not None,
        }
        description = get_description_for_source(row, source, ebay_client=ebay_client)
        placeholder_rows = find_description_placeholders(description)

        matched = {}
        for issue_key in issue_keys:
            if requested_measurements[issue_key] and placeholder_rows[issue_key]:
                matched[issue_key] = placeholder_rows[issue_key]

        if not matched:
            continue

        opt = parse_json(row["optimization"])
        result = {
            "sku": row["sku"],
            "title": (opt.get("title") or row["title"] or "")[:120],
            "source": source,
            "matched_issues": matched,
        }
        if "dimensions" in matched:
            result["trusted_dimensions"] = [
                source_dims.get("length"),
                source_dims.get("width"),
                source_dims.get("height"),
            ]
        if "weight" in matched:
            result["trusted_weight"] = source_dims.get("weight")
        results.append(result)

    return results


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan published listings for exact description placeholder rows.")
    parser.add_argument("--source", choices=["db", "live"], default="db", help="Scan local optimization.description or live eBay descriptions.")
    parser.add_argument("--issue", choices=["dimensions", "weight", "both"], default="both", help="Issue type to scan for.")
    parser.add_argument("--sku", action="append", help="Specific SKU(s) to scan. Repeat or pass comma-separated values.")
    parser.add_argument("--input-report", help="Optional prior scan report. When provided, only SKUs from that report are scanned.")
    parser.add_argument("--limit", type=int, help="Optional max number of published rows to scan.")
    parser.add_argument("--report", help="Optional custom JSON report path.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = create_parser()
    args = parser.parse_args(argv)
    sku_filter = expand_sku_filter(args.sku)
    report_skus = load_skus_from_report(args.input_report) if args.input_report else []
    sku_filter = merge_sku_filters(report_skus, sku_filter)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    if should_use_empty_filter(args.input_report, sku_filter):
        rows = []
    else:
        rows = load_published_rows(conn, sku_filter=sku_filter or None, limit=args.limit)

    ebay_client = None
    if args.source == "live" and rows:
        from src.clients.real_ebay_client import create_real_ebay_client

        ebay_client = create_real_ebay_client(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
        if not ebay_client.oauth.is_authorized():
            raise RuntimeError("eBay is not authorized")

    results = iter_issue_results(rows, args.source, args.issue, ebay_client=ebay_client)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = Path(args.report) if args.report else LOG_DIR / f"description_placeholder_scan_{args.source}_{args.issue}_{timestamp}.json"
    report = {
        "generated_at": datetime.now().isoformat(),
        "source": args.source,
        "issue": args.issue,
        "input_report": args.input_report,
        "scanned_rows": len(rows),
        "match_count": len(results),
        "matches": results,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Scanned rows: {len(rows)}")
    print(f"Matches: {len(results)}")
    print(f"Report: {report_path}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())