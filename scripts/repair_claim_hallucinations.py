"""Reset ClaimDiff hallucination SKUs back to COLLECTED. Dry-run by default."""

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

DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / "listing_audit_fix_20260620_113024.json"
_LIVE_STATUSES = {"PUBLISHED", "READY"}


def _utcnow_naive() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reset SKUs from a listing-audit hallucination report to COLLECTED."
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
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help="Audit JSON that lists the SKUs to consider.",
    )
    return parser


def _load_skus(log_file: Path) -> list[str]:
    if not log_file.is_file():
        print(f"Log file not found: {log_file}")
        return []
    with log_file.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    issues = data.get("issues", [])
    skus: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        sku = str((issue or {}).get("sku") or "").strip()
        if not sku or sku in seen:
            continue
        seen.add(sku)
        skus.append(sku)
    return skus


def _is_live(status: str, listing_id: str | None) -> bool:
    if str(status or "").strip().upper() in _LIVE_STATUSES:
        return True
    return bool(str(listing_id or "").strip())


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db is not None else Path(DATABASE_PATH)
    skus_to_repair = _load_skus(Path(args.log_file))
    if not skus_to_repair:
        print("No SKUs found in the log.")
        return 0

    print(f"Found {len(skus_to_repair)} impacted SKUs in log.")
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in skus_to_repair)
        rows = conn.execute(
            f"SELECT sku, status, listing_id, logs FROM collected_products WHERE sku IN ({placeholders})",
            skus_to_repair,
        ).fetchall()
        matched = [dict(row) for row in rows]
        print(f"Found {len(matched)} of those SKUs in the collected_products database.")

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
                f"repair_claim_hallucinations reset at {now} (prev_status={row['status']})"
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

    print("Please run batch_analyze.py in the background to continue processing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
