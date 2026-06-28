"""CLI for CRO relist/final-delist lifecycle candidates.

This script is intentionally local-only. It writes SQLite lifecycle rows or
magic-link pending rows only when --apply is passed; --dry-run uses a temporary
database copy.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import cro_delist  # noqa: E402
from src.services.cro_relist_lifecycle import (  # noqa: E402
    DEFAULT_DB,
    detect_candidates,
    approve_actions,
    precheck_approved,
    execute_prechecked_relist,
    execute_approved,
    evaluate_observations,
)


def _json_default(value: Any) -> str:
    return str(value)


def _write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _write_html(path: Path, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


def _copy_db_for_dry_run(db_path: Path) -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="cro_lifecycle_"))
    tmp_db = tmp_dir / db_path.name
    if db_path.exists():
        with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(tmp_db)) as target:
            source.backup(target)
    else:
        tmp_db.touch()
    return tmp_db


def _cleanup_dry_run_db(tmp_db: Path) -> None:
    tmp_dir = tmp_db.parent
    try:
        shutil.rmtree(tmp_dir)
    except OSError:
        pass


def _summarize_candidates(detect_report: dict[str, Any]) -> dict[str, Any]:
    candidates = detect_report.get("candidates") or []
    by_action = Counter(row.get("action_type") for row in candidates)
    by_priority = Counter(
        f"{row.get('action_type')}:{row.get('priority')}" for row in candidates
    )
    slim = dict(detect_report)
    slim["by_action"] = dict(sorted(by_action.items()))
    slim["by_action_priority"] = dict(sorted(by_priority.items()))
    slim["sample"] = candidates[:20]
    slim.pop("candidates", None)
    return slim


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate CRO relist/final-delist lifecycle candidates safely."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--detect", action="store_true", help="Detect lifecycle candidates")
    mode.add_argument("--approve", action="store_true", help="Approve all currently detected candidates")
    mode.add_argument("--precheck", action="store_true", help="Precheck approved candidates")
    mode.add_argument("--execute", action="store_true", help="Execute prechecked relist actions")
    mode.add_argument("--evaluate", action="store_true", help="Evaluate observing candidates")
    mode.add_argument(
        "--delist-links",
        action="store_true",
        help="Generate final-delist magic-link preview/pending rows",
    )

    safety = parser.add_mutually_exclusive_group(required=True)
    safety.add_argument(
        "--dry-run",
        action="store_true",
        help="Run on a temporary database copy; original DB is not changed",
    )
    safety.add_argument(
        "--apply",
        action="store_true",
        help="Write lifecycle rows or magic-link pending rows to the selected DB",
    )

    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--snapshot-date")
    parser.add_argument("--min-age-days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--html", type=Path)
    return parser


def run_cli(argv: list[str] | None = None) -> dict[str, Any]:
    parser = _build_parser()
    args = parser.parse_args(argv)

    source_db = Path(args.db)
    effective_db = source_db
    temp_db: Path | None = None
    if args.dry_run:
        temp_db = _copy_db_for_dry_run(source_db)
        effective_db = temp_db

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_db": str(source_db),
        "effective_db": str(effective_db),
        "applied": bool(args.apply),
    }

    try:
        if args.detect:
            detect_report = detect_candidates(
                snapshot_date=args.snapshot_date,
                min_age_days=args.min_age_days,
                limit=args.limit,
                db_path=effective_db,
            )
            report["mode"] = "detect"
            report["detect"] = _summarize_candidates(detect_report)
        elif args.approve:
            # We need to find candidate IDs to approve. This is a bit hacky for the CLI,
            # but we'll fetch all candidate IDs up to limit.
            with sqlite3.connect(str(effective_db)) as conn:
                rows = conn.execute(
                    "SELECT id FROM cro_listing_lifecycle_actions WHERE status = 'candidate' LIMIT ?",
                    (args.limit,)
                ).fetchall()
                action_ids = [row[0] for row in rows]
            approve_report = approve_actions(
                action_ids=action_ids,
                operator="cli",
                db_path=effective_db,
            )
            report["mode"] = "approve"
            report["approve"] = approve_report
        elif args.precheck:
            precheck_report = precheck_approved(
                limit=args.limit,
                apply_changes=args.apply,
                operator="cli",
                db_path=effective_db,
            )
            report["mode"] = "precheck"
            report["precheck"] = precheck_report
        elif args.execute:
            execute_report = execute_prechecked_relist(
                limit=args.limit,
                apply_changes=args.apply,
                operator="cli",
                db_path=effective_db,
            )
            report["mode"] = "execute"
            report["execute"] = execute_report
        elif args.evaluate:
            evaluate_report = evaluate_observations(
                snapshot_date=args.snapshot_date,
                db_path=effective_db,
            )
            report["mode"] = "evaluate"
            report["evaluate"] = evaluate_report
        elif args.delist_links:
            link_report = cro_delist.build_magic_links(
                base_url=args.base_url,
                db_path=effective_db,
                limit=args.limit,
            )
            report["mode"] = "delist_links"
            report["delist_links"] = link_report
            if args.html:
                _write_html(args.html, cro_delist.render_email_html(link_report))
                report["html_path"] = str(args.html)

        if args.out:
            _write_json(args.out, report)
            report["out_path"] = str(args.out)
        return report
    finally:
        if temp_db is not None:
            _cleanup_dry_run_db(temp_db)


def main(argv: list[str] | None = None) -> None:
    report = run_cli(argv)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
