"""Reset READY/PUBLISHED rows whose descriptions lack KEY FEATURES / lists.

Dry-run by default. Live rows are refused unless --include-live is given.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.db.collection_db import DATABASE_PATH
from src.db.database_safety import DatabaseSafetyError, assert_runtime_not_in_maintenance
from src.utils.logs_codec import append_log

_LIVE_STATUSES = {"PUBLISHED", "READY"}


def _utcnow_naive() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reset SKUs with empty/missing listing descriptions to COLLECTED."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Also requires --confirm-skus matching the writable SKU count.",
    )
    parser.add_argument(
        "--confirm-skus",
        type=int,
        help="Must equal the number of rows that will actually be written (excludes refused live rows).",
    )
    parser.add_argument(
        "--include-live",
        action="store_true",
        help="Allow resetting PUBLISHED/READY rows or rows that still have a listing_id.",
    )
    parser.add_argument("--db", type=Path, default=None, help="SQLite path (defaults to DATABASE_PATH).")
    parser.add_argument(
        "--maintenance-lock",
        type=Path,
        default=None,
        help="Path of logs/_maintenance.lock. --apply refuses if this file exists.",
    )
    return parser


def _is_live(status: str, listing_id: str | None) -> bool:
    if str(status or "").strip().upper() in _LIVE_STATUSES:
        return True
    return bool(str(listing_id or "").strip())


def _missing_description(optimization_raw: str | None) -> bool:
    try:
        opt = json.loads(optimization_raw) if optimization_raw else {}
        if not isinstance(opt, dict):
            return False
        raw_desc = opt.get("description", "")
        if raw_desc is None:
            desc = ""
        elif isinstance(raw_desc, str):
            desc = raw_desc
        else:
            desc = str(raw_desc)
        has_highlights = "KEY FEATURES" in desc
        has_list = "<li" in desc.lower()
        return not has_highlights or not has_list
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db is not None else Path(DATABASE_PATH)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT sku, status, listing_id, optimization, logs FROM collected_products "
            "WHERE status IN ('READY', 'PUBLISHED')"
        ).fetchall()

        matched = [dict(row) for row in rows if _missing_description(row["optimization"])]
        if not matched:
            print("No impacted SKUs found.")
            return 0

        print(f"Found {len(matched)} impacted SKUs.")
        for row in matched:
            print(f"  {row['sku']}: status={row['status']} listing_id={row['listing_id'] or ''}")

        refused = []
        eligible = []
        for row in matched:
            if (not args.include_live) and _is_live(row["status"], row["listing_id"]):
                refused.append(row)
            else:
                eligible.append(row)

        if refused:
            print(f"Refused {len(refused)} live row(s) (PUBLISHED/READY or listing_id set):")
            for row in refused:
                print(f"  refuse {row['sku']} status={row['status']} listing_id={row['listing_id'] or ''}")

        print(
            f"counts: matched={len(matched)} refused_live={len(refused)} writable={len(eligible)}"
        )

        if not args.apply:
            print("Dry-run: no writes. Pass --apply --confirm-skus <n> to write.")
            return 0

        lock = Path(args.maintenance_lock) if args.maintenance_lock is not None else PROJECT_ROOT / "logs" / "_maintenance.lock"
        try:
            assert_runtime_not_in_maintenance(lock)
        except DatabaseSafetyError as exc:
            print(f"{exc}; writing nothing.")
            return 2

        if args.confirm_skus != len(eligible):
            print(
                f"--confirm-skus {args.confirm_skus!r} does not match writable SKU count {len(eligible)}; writing nothing."
            )
            return 2

        now = _utcnow_naive()
        for row in eligible:
            log_entry = (
                f"repair_empty_descriptions reset at {now} (prev_status={row['status']})"
            )
            conn.execute(
                """
                UPDATE collected_products
                SET status = 'COLLECTED',
                    optimization = NULL,
                    updated_at = ?,
                    logs = ?
                WHERE sku = ?
                """,
                (now, append_log(row["logs"], log_entry), row["sku"]),
            )
            print(f"Reset {row['sku']} to COLLECTED.")
        conn.commit()
    finally:
        conn.close()

    print("Please run batch_analyze.py (and audit_fix_ready_drafts.py) manually if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
