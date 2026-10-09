#!/usr/bin/env python3
"""Preview or apply the runtime reports/logs retention policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.report_retention import cleanup_runtime_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="delete expired whitelisted artifacts; default is dry-run",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="preview expired artifacts without deleting them (default)",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="project root containing reports/ and logs/",
    )
    args = parser.parse_args(argv)

    summary = cleanup_runtime_artifacts(
        project_root=args.project_root,
        dry_run=not args.apply,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
