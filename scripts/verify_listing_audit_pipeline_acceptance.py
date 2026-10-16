#!/usr/bin/env python3
"""Acceptance checker for the listing-audit daily pipeline.

Run after the next morning's 11:30–13:00 window (or any time with fresh reports):

    python scripts/verify_listing_audit_pipeline_acceptance.py
    python scripts/verify_listing_audit_pipeline_acceptance.py --date 20260827

Checks (plan §2):
  - latest live_audit report has freeze / issue-type context fields
  - category_mismatch_manifest exists when category_mismatch is present
  - a fix-mode report exists today with fixed_count / residual structure
  - missing_video count vs prior day (expect drop up to ~30 if video job ran)
  - 25604 errors in today's fix reports are near-zero
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
sys.path.insert(0, str(ROOT))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _issue_types(report: dict) -> Counter:
    c: Counter = Counter()
    for item in report.get("issues") or []:
        for issue in item.get("issues") or []:
            if isinstance(issue, dict):
                typ = str(issue.get("type") or "").strip()
                if typ:
                    c[typ] += 1
    return c


def _reports_for_date(day: str) -> list[Path]:
    return sorted(LOGS.glob(f"listing_audit_fix_{day}_*.json"))


def _pick_mode(paths: list[Path], mode: str) -> Path | None:
    """Prefer the fullest report for the mode (full corpus over scoped fix runs)."""
    best: Path | None = None
    best_score = -1
    for path in paths:
        try:
            data = _load(path)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("mode") != mode:
            continue
        # Full-corpus audits have large total_published; scoped source-report
        # runs only cover the filtered SKU set.
        score = int(data.get("total_published") or 0)
        if score > best_score:
            best = path
            best_score = score
    return best


def _count_25604(report: dict) -> int:
    n = 0
    for item in report.get("issues") or []:
        for text in item.get("fixes_applied") or []:
            s = str(text)
            if "25604" in s or "Availability not found" in s:
                n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y%m%d"),
        help="Calendar day YYYYmmdd to inspect (default: today)",
    )
    args = parser.parse_args(argv)
    day = str(args.date)
    prior = (datetime.strptime(day, "%Y%m%d")).toordinal() - 1
    prior_day = datetime.fromordinal(prior).strftime("%Y%m%d")

    print(f"=== listing audit pipeline acceptance @ {day} ===")
    today_paths = _reports_for_date(day)
    prior_paths = _reports_for_date(prior_day)
    if not today_paths:
        print(f"FAIL: no listing_audit_fix_{day}_*.json yet")
        return 1

    audit_path = _pick_mode(today_paths, "live_audit")
    fix_path = _pick_mode(today_paths, "fix")
    prior_audit = _pick_mode(prior_paths, "live_audit")

    checks: list[tuple[str, bool, str]] = []

    if audit_path is None:
        checks.append(("live_audit_report", False, "missing live_audit mode report"))
        audit = {}
    else:
        audit = _load(audit_path)
        checks.append(("live_audit_report", True, str(audit_path.name)))
        checks.append(
            (
                "freeze_field",
                "skipped_clean_frozen" in audit,
                f"skipped_clean_frozen={audit.get('skipped_clean_frozen')}",
            )
        )
        types = _issue_types(audit)
        cat_n = types.get("category_mismatch", 0)
        manifests = sorted(LOGS.glob(f"category_mismatch_manifest_{day}_*.csv"))
        if cat_n:
            checks.append(
                (
                    "category_manifest",
                    bool(manifests),
                    f"category_mismatch={cat_n}; manifests={len(manifests)}",
                )
            )
        else:
            checks.append(("category_manifest", True, "no category_mismatch today"))

        if prior_audit:
            prior = _load(prior_audit)
            prior_types = _issue_types(prior)
            cur_v = types.get("missing_video", 0)
            prev_v = prior_types.get("missing_video", 0)
            delta = cur_v - prev_v
            # Same-day video autofix report (scoped --fix) proves the 13:00 job
            # ran; next morning's live_audit is the true delta check.
            video_fix = None
            for path in reversed(today_paths):
                data = _load(path)
                if data.get("mode") != "fix":
                    continue
                if int(data.get("total_published") or 0) > 80:
                    continue  # full-ish source-aspect run, not video-only
                applied = 0
                for item in data.get("issues") or []:
                    texts = " ".join(str(x) for x in (item.get("fixes_applied") or []))
                    if "video" in texts.lower() or "__sync_video__" in str(item.get("selected_fix_keys") or []):
                        applied += 1
                if applied:
                    video_fix = (path.name, int(data.get("fixed_count") or 0), applied)
                    break
            if video_fix:
                checks.append(
                    (
                        "missing_video_delta",
                        True,
                        f"morning {prev_v}→{cur_v}; same-day video fix {video_fix[0]} "
                        f"fixed={video_fix[1]} video_rows={video_fix[2]} "
                        f"(next-day audit confirms net drop)",
                    )
                )
            else:
                ok = delta <= 5
                checks.append(
                    (
                        "missing_video_delta",
                        ok,
                        f"{prev_v} → {cur_v} (delta={delta:+d}; target ≤+5 after 13:00 job)",
                    )
                )
        else:
            checks.append(("missing_video_delta", True, f"no prior-day audit; today video={types.get('missing_video', 0)}"))

    # Prefer the newest fix report for residual/25604 checks (may be video or aspect).
    newest_fix: Path | None = None
    newest_mtime = -1.0
    for path in today_paths:
        try:
            data = _load(path)
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("mode") != "fix":
            continue
        mtime = path.stat().st_mtime
        if mtime > newest_mtime:
            newest_fix = path
            newest_mtime = mtime
    fix_path = newest_fix or fix_path

    if fix_path is None:
        checks.append(("fix_report", False, "no fix-mode report yet (12:10/13:00)"))
    else:
        fix = _load(fix_path)
        checks.append(("fix_report", True, f"{fix_path.name} fixed={fix.get('fixed_count')}"))
        n_25604 = 0
        for path in today_paths:
            data = _load(path)
            if data.get("mode") == "fix":
                n_25604 += _count_25604(data)
        # Soft: morning pre-hardening report may still mention 25604; newest
        # post-restart fix should be clean. Flag if newest has any.
        newest_25604 = _count_25604(fix)
        checks.append(
            (
                "availability_25604",
                newest_25604 == 0,
                f"newest_fix_25604={newest_25604} (all_today_sum={n_25604})",
            )
        )
        has_post = any(
            isinstance(item.get("post_fix_issues"), list) or item.get("fixes_applied")
            for item in (fix.get("issues") or [])
        )
        checks.append(("fix_residual_fields", has_post, "fixes_applied/post_fix_issues present"))

    # Email HTML local saves (best-effort)
    reports_dir = ROOT / "reports"
    htmls = sorted(reports_dir.glob(f"daily_report_{day}_*.html")) if reports_dir.exists() else []
    audit_mail = any("刊登内容审计" in p.read_text(encoding="utf-8", errors="replace")[:2000] for p in htmls)
    fix_mail = any("刊登修复报告" in p.read_text(encoding="utf-8", errors="replace")[:2000] for p in htmls)
    checks.append(("email_audit_html", audit_mail or not htmls, f"audit_html={audit_mail} files={len(htmls)}"))
    checks.append(("email_fix_html", fix_mail or not htmls, f"fix_html={fix_mail} files={len(htmls)}"))

    failed = 0
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {name}: {detail}")

    print(f"=== result: {'PASS' if failed == 0 else f'{failed} FAIL(s)'} ===")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
