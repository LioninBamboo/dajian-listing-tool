"""Run a limited Phase 3 revive-relist batch.

Executes prechecked revive actions one at a time so the run can stop if an old
listing was withdrawn but the replacement listing failed to publish.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.cro_relist_lifecycle import (  # noqa: E402
    ACTION_REVIVE_RELIST,
    STATUS_PRECHECKED,
    execute_prechecked_relist,
)


def _counts(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM cro_listing_lifecycle_actions
                WHERE action_type = ?
                GROUP BY status
                ORDER BY status
                """,
                (ACTION_REVIVE_RELIST,),
            )
        ]


def _next_action(db_path: Path) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, sku, old_listing_id
            FROM cro_listing_lifecycle_actions
            WHERE action_type = ? AND status = ?
            ORDER BY id
            LIMIT 1
            """,
            (ACTION_REVIVE_RELIST, STATUS_PRECHECKED),
        ).fetchone()
        return dict(row) if row else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--db", type=Path, default=ROOT / "ebay_collection.db")
    parser.add_argument("--operator", default="codex_phase3_limited")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--backup", default="")
    args = parser.parse_args()

    db_path = args.db.resolve()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = args.report_dir / f"cro_phase3_relist_{run_id}.json"

    report: dict[str, Any] = {
        "run_id": run_id,
        "db_path": str(db_path),
        "backup": args.backup,
        "operator": args.operator,
        "limit": args.limit,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "before_counts": _counts(db_path),
        "steps": [],
        "stop_reason": "",
    }

    print(f"[phase3] start limit={args.limit} db={db_path}", flush=True)
    for index in range(max(0, args.limit)):
        action = _next_action(db_path)
        if not action:
            report["stop_reason"] = "no_prechecked_actions"
            print("[phase3] no prechecked actions left", flush=True)
            break

        print(
            "[phase3] step "
            f"{index + 1}/{args.limit} sku={action['sku']} old={action['old_listing_id']}",
            flush=True,
        )
        result = execute_prechecked_relist(
            limit=1,
            apply_changes=True,
            operator=args.operator,
            db_path=db_path,
        )
        report["steps"].append({"action": action, "result": result})
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        rows = result.get("results") or []
        first = rows[0] if rows else {}
        print(
            "[phase3] result "
            f"status={first.get('status')} old_withdrawn={first.get('old_withdrawn', False)} "
            f"new={first.get('new_listing_id', '')} error={first.get('error', '')}",
            flush=True,
        )
        if first.get("status") == "failed":
            if first.get("old_withdrawn"):
                report["stop_reason"] = "failed_after_old_withdrawn"
                print("[phase3] stopping: old listing was withdrawn but publish failed", flush=True)
            else:
                report["stop_reason"] = "failed_before_old_withdrawn"
                print("[phase3] stopping: unexpected failure before old listing withdrawal", flush=True)
            break
    else:
        report["stop_reason"] = "limit_reached"

    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["after_counts"] = _counts(db_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[phase3] report={report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
