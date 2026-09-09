"""Dry-run or apply the initial QC experience registry seed."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.qc_experience_migration import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_DB_PATH,
    run_qc_experience_seed_migration,
)


def _default_report_path(timestamp: str | None) -> Path:
    stamp = timestamp or datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "logs" / f"qc_experience_seed_migration_{stamp}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--timestamp", help="YYYYMMDD_HHMMSS; useful for deterministic runs")
    parser.add_argument("--maintenance-lock", type=Path, default=PROJECT_ROOT / "logs" / "_maintenance.lock")
    parser.add_argument("--report", type=Path, help="JSON report path; defaults to logs/qc_experience_seed_migration_*.json")
    parser.add_argument("--apply", action="store_true", help="write the additive registry seed after creating a backup")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_qc_experience_seed_migration(
            args.db,
            args.backup_dir,
            apply=args.apply,
            timestamp=args.timestamp,
            maintenance_lock=args.maintenance_lock,
        )
    except Exception as exc:
        print(f"QC experience seed migration failed: {exc}", file=sys.stderr)
        return 2

    report_path = args.report or _default_report_path(args.timestamp)
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**result, "report_path": str(report_path)}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
