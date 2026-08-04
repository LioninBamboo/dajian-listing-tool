#!/usr/bin/env python3
"""Verify the saved QC plan evidence artifacts without modifying live data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.listing_qc_plan_verification import build_plan_evidence_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--dry-run", required=True, dest="dry_run")
    parser.add_argument("--ready-audit", required=True)
    parser.add_argument("--critical-audit", required=True)
    parser.add_argument("--detect-only", required=True)
    parser.add_argument("--cross-system")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    report = build_plan_evidence_report(
        allowlist_path=args.allowlist,
        dry_run_path=args.dry_run,
        ready_audit_path=args.ready_audit,
        critical_audit_path=args.critical_audit,
        detect_only_path=args.detect_only,
        cross_system_path=args.cross_system,
    )
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "dry_run": report["dry_run_verification"], "followups": report["followups"]}, ensure_ascii=False))
    return 0 if report["status"] != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())

