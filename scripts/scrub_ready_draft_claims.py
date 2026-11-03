#!/usr/bin/env python3
"""Scrub unsupported claims from local READY drafts. Dry-run by default.

Reuses existing factsheet / quality-gate / claim-diff detectors. Writes only
local ERP ``collected_products.optimization`` rows when ``--apply`` is set.
Never calls eBay, never writes ``ebay_tokens.db`` or ``.env``.
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def force_utf8_stdio() -> None:
    """UTF-8 stdout/stderr on a GBK Windows console."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
                continue
            except (OSError, ValueError):
                pass
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            setattr(
                sys,
                name,
                io.TextIOWrapper(buffer, encoding="utf-8", errors="replace", line_buffering=True),
            )


force_utf8_stdio()

from src.db.collection_db import DATABASE_PATH
from src.utils.ready_draft_claim_scrub import (
    apply_scrub_to_row,
    backup_rows,
    connect_collection_db,
    format_diff_lines,
    is_live_row,
    is_ready_unpublished,
    retry_locked,
    row_to_candidate_source,
    scrub_candidate,
)
from src.utils.store_profile import get_store_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrub unsupported claims from READY / unpublished ERP drafts."
    )
    parser.add_argument("--sku", action="append", default=[], help="SKU to scrub (repeatable).")
    parser.add_argument("--skus-file", type=Path, help="UTF-8 file of SKUs, one per line.")
    parser.add_argument(
        "--store",
        default="",
        help="Store label for the report (main/gp/aqua). Uses this instance's local ERP DB.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite path. Quote paths with spaces on Windows.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write local READY/unpublished draft rows only. Refuses PUBLISHED/live.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "reports",
        help="Directory for the JSON report (and apply-time row backup).",
    )
    return parser


def _load_sku_args(args: argparse.Namespace) -> list[str]:
    skus: list[str] = []
    seen: set[str] = set()
    for raw in list(args.sku or []):
        sku = str(raw or "").strip()
        if sku and sku not in seen:
            seen.add(sku)
            skus.append(sku)
    if args.skus_file is not None:
        text = Path(args.skus_file).read_text(encoding="utf-8")
        for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
            sku = line.strip()
            if sku and not sku.startswith("#") and sku not in seen:
                seen.add(sku)
                skus.append(sku)
    return skus


def _select_rows(conn: sqlite3.Connection, skus: list[str]) -> list[sqlite3.Row]:
    if skus:
        placeholders = ",".join("?" for _ in skus)
        sql = f"""
            SELECT sku, status, listing_id, title, description, attributes, specs,
                   images, videos, optimization, logs, updated_at
            FROM collected_products
            WHERE sku IN ({placeholders})
        """
        return list(retry_locked(lambda: conn.execute(sql, skus).fetchall()))
    sql = """
        SELECT sku, status, listing_id, title, description, attributes, specs,
               images, videos, optimization, logs, updated_at
        FROM collected_products
        WHERE UPPER(status) IN ('READY', 'READY_TO_PUBLISH')
    """
    return list(retry_locked(lambda: conn.execute(sql).fetchall()))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db is not None else Path(DATABASE_PATH)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    skus = _load_sku_args(args)
    store = str(args.store or "").strip() or getattr(get_store_profile(), "brand_name", "")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    mode = "apply" if args.apply else "dry-run"
    report: dict = {
        "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "mode": mode,
        "db_path": str(db_path),
        "store": store,
        "skus": skus,
        "results": [],
        "counts": {},
        "degraded": False,
    }
    exit_code = 0

    try:
        conn = connect_collection_db(str(db_path))
    except Exception as exc:
        print(f"degraded: cannot open db {db_path}: {exc}", file=sys.stderr)
        report["degraded"] = True
        report["error"] = str(exc)
        _write_report(report_dir, stamp, mode, report)
        return 2

    try:
        try:
            rows = _select_rows(conn, skus)
        except sqlite3.OperationalError as exc:
            print(f"degraded: select failed: {exc}", file=sys.stderr)
            report["degraded"] = True
            report["error"] = str(exc)
            exit_code = 2
            rows = []

        seen_skus = {str(row["sku"]) for row in rows}
        for sku in skus:
            if sku not in seen_skus:
                report["results"].append(
                    {
                        "sku": sku,
                        "status": "missing",
                        "before_blockers": [],
                        "after_blockers": [],
                        "diffs": [],
                    }
                )

        for row in rows:
            sku = str(row["sku"])
            status = str(row["status"] or "")
            listing_id = row["listing_id"] or ""
            if is_live_row(status, listing_id):
                report["results"].append(
                    {
                        "sku": sku,
                        "status": "refused_live",
                        "row_status": status,
                        "listing_id": listing_id,
                        "before_blockers": [],
                        "after_blockers": [],
                        "diffs": [],
                    }
                )
                print(f"SKU {sku} refused_live status={status} listing_id={listing_id}")
                continue
            if skus and not is_ready_unpublished(status, listing_id):
                report["results"].append(
                    {
                        "sku": sku,
                        "status": "refused_live" if is_live_row(status, listing_id) else "not_ready",
                        "row_status": status,
                        "listing_id": listing_id,
                        "before_blockers": [],
                        "after_blockers": [],
                        "diffs": [],
                    }
                )
                print(f"SKU {sku} not writable status={status}")
                continue
            try:
                candidate, source = row_to_candidate_source(row)
                if not candidate.get("title") and not candidate.get("description"):
                    result_status = "needs_manual"
                    payload = {
                        "sku": sku,
                        "status": result_status,
                        "row_status": status,
                        "listing_id": listing_id,
                        "needs_manual_reason": "no generated copy",
                        "before_blockers": [],
                        "after_blockers": [],
                        "diffs": [],
                    }
                    report["results"].append(payload)
                    print(f"SKU {sku} needs_manual: no generated copy")
                    continue
                _scrubbed, result = scrub_candidate(candidate, source, sku=sku)
                result.row_status = status
                result.listing_id = str(listing_id or "")
                report["results"].append(result.to_dict())
                for line in format_diff_lines(result, source):
                    print(line)
            except Exception as exc:
                report["degraded"] = True
                exit_code = 2
                report["results"].append(
                    {
                        "sku": sku,
                        "status": "degraded",
                        "error": str(exc),
                        "before_blockers": [],
                        "after_blockers": [],
                        "diffs": [],
                    }
                )
                print(f"SKU {sku} degraded: {exc}", file=sys.stderr)

        if args.apply:
            apply_targets = [
                item for item in report["results"]
                if item.get("status") in {"cleaned", "needs_manual"}
                and item.get("sku")
                and item.get("diffs")
            ]
            by_sku = {str(row["sku"]): row for row in rows}
            to_write = [
                by_sku[item["sku"]]
                for item in apply_targets
                if item["sku"] in by_sku
                and is_ready_unpublished(by_sku[item["sku"]]["status"], by_sku[item["sku"]]["listing_id"])
                and item.get("diffs")
            ]
            if to_write:
                backup_path = report_dir / f"scrub_ready_draft_claims_backup_{stamp}.json"
                try:
                    backup_rows(to_write, backup_path)
                    report["backup_path"] = str(backup_path)
                except Exception as exc:
                    print(f"degraded: backup failed, writing nothing: {exc}", file=sys.stderr)
                    report["degraded"] = True
                    _write_report(report_dir, stamp, mode, report)
                    conn.close()
                    return 2
                written = 0
                for item in apply_targets:
                    if not item.get("diffs"):
                        continue
                    row = by_sku.get(item["sku"])
                    if row is None:
                        continue
                    try:
                        candidate, source = row_to_candidate_source(row)
                        scrubbed, _again = scrub_candidate(candidate, source, sku=item["sku"])
                        apply_scrub_to_row(conn, row, scrubbed)
                        written += 1
                    except Exception as exc:
                        report["degraded"] = True
                        exit_code = 2
                        print(f"SKU {item['sku']} apply failed: {exc}", file=sys.stderr)
                report["written"] = written
            else:
                report["written"] = 0
        else:
            print("Dry-run: no writes. Pass --apply to write local READY drafts only.")
    finally:
        conn.close()

    counts: dict[str, int] = {}
    for item in report["results"]:
        counts[item.get("status") or "unknown"] = counts.get(item.get("status") or "unknown", 0) + 1
    report["counts"] = counts
    _write_report(report_dir, stamp, mode, report)
    if report["degraded"] and exit_code == 0:
        return 2
    return exit_code


def _write_report(report_dir: Path, stamp: str, mode: str, report: dict) -> Path:
    path = report_dir / f"scrub_ready_draft_claims_{mode}_{stamp}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
        newline="\n",
    )
    print(f"report: {path}")
    return path


if __name__ == "__main__":
    sys.exit(main())
