"""Run read-only QC experience replay and recurrence reports."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.services.qc_experience_replay import (  # noqa: E402
    DEFAULT_CLEAN_PATHS,
    DEFAULT_DB_PATH,
    DEFAULT_HISTORICAL_PATHS,
    build_experience_recurrence_report,
    build_historical_replay_report,
)


def _stamp(value: str | None) -> str:
    return value or datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--historical", type=Path, action="append", default=None)
    parser.add_argument("--clean", type=Path, action="append", default=None)
    parser.add_argument("--timestamp")
    parser.add_argument("--replay-report", type=Path)
    parser.add_argument("--recurrence-report", type=Path)
    return parser


def _write_json(path: Path, payload: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stamp = _stamp(args.timestamp)
    historical = args.historical or list(DEFAULT_HISTORICAL_PATHS)
    clean = args.clean or list(DEFAULT_CLEAN_PATHS)
    replay = build_historical_replay_report(
        db_path=args.db,
        historical_paths=historical,
        clean_paths=clean,
    )
    recurrence = build_experience_recurrence_report(
        db_path=args.db,
        historical_paths=historical,
        clean_paths=clean,
    )
    replay_path = args.replay_report or PROJECT_ROOT / "logs" / f"qc_experience_replay_{stamp}.json"
    recurrence_path = args.recurrence_report or PROJECT_ROOT / "logs" / f"qc_experience_recurrence_{stamp}.json"
    _write_json(replay_path, replay)
    _write_json(recurrence_path, recurrence)
    print(
        json.dumps(
            {
                "replay_report": str(Path(replay_path).resolve()),
                "recurrence_report": str(Path(recurrence_path).resolve()),
                "replay_summary": replay["summary"],
                "recurrence_summary": recurrence["summary"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
