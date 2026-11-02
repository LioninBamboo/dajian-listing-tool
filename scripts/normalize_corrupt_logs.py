"""Opt-in repair for collected_products.logs cells that are JSON + trailing text.

Dry-run by default. --apply needs --store, --confirm-count, a maintenance-lock
check, and a fresh backup. Never run against a live store without Sergey's yes.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.collection_db import DATABASE_PATH
from src.db.database_safety import assert_runtime_not_in_maintenance
from src.utils.db_backup import create_online_backup
from src.utils.logs_codec import dump_logs, parse_logs

STORE_CHOICES = ("main", "gp", "aqua")
_XML_DECL = re.compile(r"<\?xml\b[^?]*\?>", re.IGNORECASE)
_XML_ELEM = re.compile(r"<([A-Za-z_][\w:.-]*)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_MARKER = re.compile(r"\]\s+([A-Z][A-Z0-9_]*)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize corrupt collected_products.logs cells to valid JSON."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write logs. Requires --store, --confirm-count, and a fresh backup.",
    )
    parser.add_argument("--store", choices=STORE_CHOICES, help="Which store this checkout is.")
    parser.add_argument(
        "--confirm-count",
        type=int,
        help="Must equal the number of corrupt rows that would be written.",
    )
    parser.add_argument("--db", type=Path, default=None, help="SQLite path (defaults to DATABASE_PATH).")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Parent directory for backups/normalize_logs_<ts>/ (defaults to PROJECT_ROOT/backups).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Directory for the JSON report (defaults to PROJECT_ROOT/logs).",
    )
    parser.add_argument(
        "--maintenance-lock",
        type=Path,
        default=None,
        help="Path of logs/_maintenance.lock (defaults to PROJECT_ROOT/logs/_maintenance.lock).",
    )
    parser.add_argument(
        "--timestamp",
        help="YYYYMMDD_HHMMSS used in backup/report names (defaults to now).",
    )
    return parser


def _elide_xml(text: str) -> str:
    elided = _XML_DECL.sub("<?xml ...?>", text)
    elided = _XML_ELEM.sub(lambda match: f"<{match.group(1)}>...</{match.group(1)}>", elided)
    return elided


def _preview_trailing(text: str, limit: int = 60) -> str:
    return _elide_xml(text)[:limit]


def _marker(trailing: str) -> str:
    match = _MARKER.search(trailing)
    return match.group(1) if match else "UNKNOWN"


def _collect_corrupt(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT sku, status, logs, updated_at FROM collected_products"
    ).fetchall()
    found: list[dict] = []
    for sku, status, logs, updated_at in rows:
        parsed = parse_logs(logs)
        if parsed.status not in {"trailing_text", "garbage"}:
            continue
        found.append(
            {
                "sku": sku,
                "status": status,
                "updated_at": updated_at,
                "old_logs": logs,
                "parsed": parsed,
                "marker": _marker(parsed.trailing),
            }
        )
    return found


def _report_lines(rows: list[dict]) -> list[str]:
    by_status = Counter(row["status"] or "" for row in rows)
    by_marker = Counter(row["marker"] for row in rows)
    lines = [
        f"corrupt_rows={len(rows)}",
        f"by_status={dict(by_status)}",
        f"by_marker={dict(by_marker)}",
    ]
    for row in rows:
        parsed = row["parsed"]
        preview = _preview_trailing(parsed.trailing)
        lines.append(
            f"{row['sku']} status={row['status']} leading_items={len(parsed.entries)} "
            f"trailing_len={len(parsed.trailing or '')} preview={preview!r}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db is not None else Path(DATABASE_PATH)
    maintenance_lock = (
        Path(args.maintenance_lock)
        if args.maintenance_lock is not None
        else PROJECT_ROOT / "logs" / "_maintenance.lock"
    )
    assert_runtime_not_in_maintenance(maintenance_lock)

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        rows = _collect_corrupt(conn)
        for line in _report_lines(rows):
            print(line)

        if not args.apply:
            print("Dry-run: no writes. Pass --apply --store {main,gp,aqua} --confirm-count <n> to write.")
            return 0

        if not args.store:
            parser.error("--apply requires --store {main,gp,aqua}")
        if args.confirm_count != len(rows):
            print(
                f"--confirm-count {args.confirm_count!r} does not match corrupt row count {len(rows)}; writing nothing."
            )
            return 2

        stamp = args.timestamp or datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        backup_parent = Path(args.backup_dir) if args.backup_dir is not None else PROJECT_ROOT / "backups"
        backup_dir = backup_parent / f"normalize_logs_{stamp}"
        backup_path = backup_dir / "ebay_collection.db"
        create_online_backup(conn, backup_path)
        check_conn = sqlite3.connect(str(backup_path), timeout=30)
        try:
            integrity = str(check_conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            check_conn.close()
        if integrity != "ok":
            print(f"Backup integrity_check={integrity!r}; writing nothing.")
            return 2

        written: list[dict] = []
        try:
            for row in rows:
                new_logs = dump_logs(row["parsed"].entries_with_trailing())
                cursor = conn.execute(
                    "UPDATE collected_products SET logs = ? WHERE sku = ? AND logs = ?",
                    (new_logs, row["sku"], row["old_logs"]),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(f"CAS missed for sku={row['sku']}")
                written.append(
                    {
                        "sku": row["sku"],
                        "status": row["status"],
                        "updated_at": row["updated_at"],
                        "marker": row["marker"],
                    }
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        readback = []
        for row in rows:
            current = conn.execute(
                "SELECT logs, status, updated_at FROM collected_products WHERE sku = ?",
                (row["sku"],),
            ).fetchone()
            parsed = parse_logs(current[0] if current else None)
            readback.append(
                {
                    "sku": row["sku"],
                    "status": current[1] if current else None,
                    "updated_at": current[2] if current else None,
                    "parse_status": parsed.status,
                    "entries": len(parsed.entries_with_trailing()),
                }
            )
    finally:
        conn.close()

    report_dir = Path(args.report_dir) if args.report_dir is not None else PROJECT_ROOT / "logs"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"normalize_corrupt_logs_{args.store}_{stamp}.json"
    report = {
        "store": args.store,
        "db": str(db_path),
        "backup": str(backup_path),
        "count": len(written),
        "written": written,
        "readback": readback,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(written)} row(s); report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
