#!/usr/bin/env python
"""Apply ONLY the fixes named in an approval manifest, one SKU at a time.

Why this exists: `audit_fix_active_listings.py --fix` with no `--fix-key`
applies EVERY pending fix for the targeted SKUs. On 2026-07-27 a run scoped
"for the assembly-flag batch" also re-categorised two indoor storage benches
into Outdoor Daybeds on live, because a categoryId fix happened to be pending
for them. The task book said not to touch categories; prose did not stop it.

So the allowlist is now mechanical. The manifest names each SKU and exactly
which fix keys are approved for it, and this script builds the scoped command
itself — the operator never composes `--fix-key` by hand and cannot widen it.

Manifest format (CSV, header required):

    sku,fix_keys,note
    W1234,Assembly Required,source says Yes
    W5678,categoryId|Item Length,approved by <name> 2026-07-28

`fix_keys` is `|`-separated. `categoryId` additionally requires
`--allow-category`, because category changes are the single most damaging
class of wrong fix in this codebase's history (indoor bench -> Outdoor
Daybeds; nightstand -> Bed Frames; console table -> Sofas; bell tent ->
Ice Chests & Coolers).

    python scripts/apply_approved_fixes.py manifest.csv            # dry run
    python scripts/apply_approved_fixes.py manifest.csv --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "scripts" / "audit_fix_active_listings.py"

# Fix keys that may never be applied from a manifest without an extra opt-in.
GUARDED_KEYS = {"categoryId", "categoryName"}


def load_manifest(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = {"sku", "fix_keys"} - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"[ERROR] manifest missing column(s): {sorted(missing)}")
        for lineno, raw in enumerate(reader, start=2):
            sku = (raw.get("sku") or "").strip()
            if not sku or sku.startswith("#"):
                continue
            keys = [k.strip() for k in (raw.get("fix_keys") or "").split("|") if k.strip()]
            if not keys:
                raise SystemExit(f"[ERROR] line {lineno}: {sku} has no fix_keys")
            rows.append({"sku": sku, "fix_keys": keys, "note": (raw.get("note") or "").strip()})
    if not rows:
        raise SystemExit("[ERROR] manifest is empty")
    return rows


def check_guarded(rows: list[dict], allow_category: bool) -> None:
    guarded = [(r["sku"], k) for r in rows for k in r["fix_keys"] if k in GUARDED_KEYS]
    if guarded and not allow_category:
        print("[BLOCKED] manifest requests guarded fix keys but --allow-category was not passed:")
        for sku, key in guarded:
            print(f"    {sku}: {key}")
        print("\nCategory changes have caused live mis-categorisation repeatedly.")
        print("Re-run with --allow-category only after a human approved each row.")
        raise SystemExit(2)


def build_command(sku: str, fix_keys: list[str], apply: bool) -> list[str]:
    # --ignore-clean-freeze is not optional here: a listing fixed earlier gets
    # frozen as "clean" and the audit then skips it entirely, so a scoped repair
    # run silently finds nothing to do (2026-07-28, W3098P470268).
    cmd = [sys.executable, str(AUDIT), "--sku", sku, "--live", "--ignore-clean-freeze", "--exit-zero-on-issues"]
    for key in fix_keys:
        cmd += ["--fix-key", key]
    if apply:
        cmd.append("--fix")
    return cmd


def _latest_report_item(sku: str) -> dict | None:
    """Return the newest audit item for ``sku``, if a report exists."""
    reports = sorted(
        (ROOT / "logs").glob("listing_audit_fix_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for report in reports[:5]:
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in data.get("issues", []):
            if item.get("sku") == sku:
                return item
    return None


def _keys_not_offered(sku: str, requested: list[str]) -> list[str]:
    """Which requested keys does the audit not actually offer for this SKU?

    A manifest key that no fix generates is filtered to nothing and the run
    reports success having changed exactly nothing — which is how three
    dimension rows were recorded as applied while live never moved
    (2026-07-28: the keys "Item Height"/"Item Width" do not exist; the real one
    is "Product Dimensions", and these SKUs offered no dimension fix at all).
    """
    item = _latest_report_item(sku)
    if item is not None:
        offered = {str(k) for k in (item.get("fix_keys") or [])}
        return [k for k in requested if k not in offered]
    return []  # no report to check against; leave the run to its own exit code


def _apply_errors_for_sku(sku: str) -> list[str]:
    """Extract errors recorded inside a successful audit process exit.

    ``--exit-zero-on-issues`` intentionally neutralizes issue-count exits, but
    an apply can still fail after the audit has completed. The JSON report is
    the authoritative result for that case.
    """
    item = _latest_report_item(sku)
    if item is None:
        return []
    return [
        str(result)
        for result in (item.get("fixes_applied") or [])
        if str(result).lstrip().startswith("ERROR")
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="CSV manifest: sku,fix_keys,note")
    parser.add_argument("--apply", action="store_true", help="actually write to eBay")
    parser.add_argument(
        "--allow-category",
        action="store_true",
        help="permit categoryId/categoryName rows (each must be human-approved)",
    )
    parser.add_argument("--stop-on-error", action="store_true", default=True)
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=3,
        help=(
            "Stop after this many CONSECUTIVE per-SKU apply errors (default 3). "
            "One SKU whose eBay availability record is missing must not strand "
            "the other 113 rows; a run where everything fails still stops fast."
        ),
    )
    args = parser.parse_args()

    # The audit's output carries the store footer (✦), emoji severity markers and
    # Chinese summaries. On Windows both the subprocess decode AND this process's
    # stdout default to cp936 and raise on them — the first silently lost the
    # child's output, the second crashed mid-report (2026-07-28).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # non-reconfigurable stream
            pass

    rows = load_manifest(Path(args.manifest))
    check_guarded(rows, args.allow_category)

    print(f"manifest: {args.manifest}")
    print(f"rows    : {len(rows)}")
    print(f"mode    : {'APPLY (writes to eBay)' if args.apply else 'DRY RUN'}")
    print()

    failures = 0
    consecutive = 0
    item_errors: list[str] = []
    for index, row in enumerate(rows, start=1):
        sku, keys = row["sku"], row["fix_keys"]
        print(f"[{index}/{len(rows)}] {sku}  keys={keys}" + (f"  # {row['note']}" if row["note"] else ""))
        cmd = build_command(sku, keys, args.apply)
        # encoding is explicit: the audit prints the store footer (✦) and Chinese
        # summaries, and Windows' default cp936 decode raised inside subprocess's
        # reader thread — the output was lost and every row still reported OK.
        result = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        tail = (result.stdout or "").strip().splitlines()[-6:]
        for line in tail:
            print(f"      {line}")
        unavailable = _keys_not_offered(sku, keys)
        if unavailable:
            failures += 1
            print(f"      ! manifest names fix keys the audit does not offer: {unavailable}")
            print(f"      ! nothing was applied for {sku} — fix the manifest, do not retry as-is")
            if args.stop_on_error:
                print(f"\n[STOP] {sku}: unusable manifest row.")
                return 1
            print()
            continue

        apply_errors = _apply_errors_for_sku(sku) if args.apply else []
        if apply_errors:
            # Per-SKU data conditions (missing eBay availability record, dead
            # offer) are item-level, not systematic: on 2026-08-10 one such SKU
            # at row 2 stopped a 115-row batch with zero writes. Record, skip,
            # keep going — a genuine systemic break shows up as a RUN of
            # consecutive failures and still halts the batch.
            failures += 1
            consecutive += 1
            for error in apply_errors:
                print(f"      ! {error}")
            item_errors.append(f"{sku}: {apply_errors[0][:120]}")
            if args.stop_on_error and consecutive >= args.max_consecutive_errors:
                print(
                    f"\n[STOP] {consecutive} consecutive apply errors "
                    f"(last: {sku}) — looks systematic, not per-item."
                )
                return 1
            print()
            continue
        consecutive = 0

        if result.returncode != 0:
            failures += 1
            err = (result.stderr or "").strip().splitlines()[-4:]
            for line in err:
                print(f"      ! {line}")
            if args.stop_on_error:
                print(f"\n[STOP] {sku} exited {result.returncode}. Nothing further attempted.")
                return 1
        print()

    print(f"done: {len(rows)} row(s), {failures} failure(s)")
    if item_errors:
        print("\nper-SKU errors (skipped, batch continued):")
        for line in item_errors:
            print(f"  - {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
