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

`fix_keys` is `|`-separated. `categoryId` / `categoryName` /
`categoryApproved` additionally require `--allow-category`, because
category changes are the single most damaging class of wrong fix in this
codebase's history (indoor bench -> Outdoor Daybeds; nightstand -> Bed
Frames; console table -> Sofas; bell tent -> Ice Chests & Coolers).

A `categoryId` / `categoryName` row always goes through the audit remap.
`--mark-category-approved` then pins the leaf that is actually stored
after a successful audit. Optional column `category_id` is the requested
target; if it is set and does not match the stored leaf after audit,
nothing is stamped.

The stamp-only shortcut (no audit) applies only when the row's sole key
is `categoryApproved` and no target `category_id` is given — the current
stored leaf is already correct:

    python scripts/apply_approved_fixes.py manifest.csv --allow-category --mark-category-approved
    python scripts/apply_approved_fixes.py manifest.csv --allow-category --mark-category-approved --apply

    python scripts/apply_approved_fixes.py manifest.csv            # dry run
    python scripts/apply_approved_fixes.py manifest.csv --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.approved_category import mark_category_approved

AUDIT = ROOT / "scripts" / "audit_fix_active_listings.py"
DEFAULT_DB = ROOT / "ebay_collection.db"

# Fix keys that may never be applied from a manifest without an extra opt-in.
GUARDED_KEYS = {"categoryId", "categoryName", "categoryApproved"}
REMAP_KEYS = frozenset({"categoryId", "categoryName"})
STAMPABLE_KEYS = frozenset(GUARDED_KEYS)


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
            rows.append(
                {
                    "sku": sku,
                    "fix_keys": keys,
                    "note": (raw.get("note") or "").strip(),
                    "category_id": str(raw.get("category_id") or "").strip(),
                }
            )
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


def _parse_optimization(raw: object) -> dict:
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def requested_category_id(row: dict) -> str:
    return str(row.get("category_id") or "").strip()


def is_stamp_only_row(row: dict) -> bool:
    """True only when the sole key is categoryApproved and no target id is given.

    A ``categoryId`` / ``categoryName`` key always goes through audit/remap.
    """
    keys = [str(k).strip() for k in (row.get("fix_keys") or []) if str(k).strip()]
    if any(k in REMAP_KEYS for k in keys):
        return False
    return bool(keys) and all(k == "categoryApproved" for k in keys) and not requested_category_id(row)


def can_stamp_after_audit(
    *,
    returncode: int,
    apply_errors: list[str] | None,
    stored_category_id: str,
    requested_id: str = "",
) -> tuple[bool, str]:
    """Stamp only the leaf stored after a successful remap."""
    if returncode != 0:
        return False, f"audit exited {returncode}"
    if apply_errors:
        return False, "audit reported apply errors"
    stored = str(stored_category_id or "").strip()
    if not stored:
        return False, "no stored categoryId after audit"
    requested = str(requested_id or "").strip()
    if requested and requested != stored:
        return False, (
            f"requested categoryId={requested} differs from stored {stored}; "
            "remap was not applied"
        )
    return True, ""


def load_stored_category_id(sku: str, db_path: Path | None = None) -> str:
    path = Path(db_path) if db_path is not None else DEFAULT_DB
    if not path.is_file():
        return ""
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT optimization FROM collected_products WHERE sku=?",
            (sku,),
        ).fetchone()
        if not row:
            return ""
        return str(_parse_optimization(row[0]).get("categoryId") or "").strip()
    finally:
        conn.close()


def stamp_local_category_approved(
    sku: str,
    *,
    apply: bool,
    db_path: Path | None = None,
    expected_category_id: str = "",
) -> dict:
    """Pin the SKU's current stored categoryId as the approved leaf.

    Used after a successful remap, or on the stamp-only shortcut when the
    stored leaf is already the approved target. Refuses to stamp when
    ``expected_category_id`` is set and does not match the stored leaf.
    """
    path = Path(db_path) if db_path is not None else DEFAULT_DB
    if not path.is_file():
        return {"ok": False, "error": f"database not found: {path}"}
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT optimization FROM collected_products WHERE sku=?",
            (sku,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"SKU {sku} not found"}
        opt = _parse_optimization(row[0])
        cid = str(opt.get("categoryId") or "").strip()
        if not cid:
            return {"ok": False, "error": f"{sku} has no stored categoryId to approve"}
        expected = str(expected_category_id or "").strip()
        if expected and expected != cid:
            return {
                "ok": False,
                "error": (
                    f"requested categoryId={expected} differs from stored {cid}; "
                    "remap was not applied"
                ),
            }
        if not apply:
            return {"ok": True, "dry_run": True, "category_id": cid}
        mark_category_approved(opt, cid)
        conn.execute(
            "UPDATE collected_products SET optimization=? WHERE sku=?",
            (json.dumps(opt, ensure_ascii=False), sku),
        )
        conn.commit()
        return {"ok": True, "applied": True, "category_id": cid}
    finally:
        conn.close()


def build_command(sku: str, fix_keys: list[str], apply: bool) -> list[str]:
    # --ignore-clean-freeze is not optional here: a listing fixed earlier gets
    # frozen as "clean" and the audit then skips it entirely, so a scoped repair
    # run silently finds nothing to do (2026-07-28, W3098P470268).
    cmd = [sys.executable, str(AUDIT), "--sku", sku, "--live", "--ignore-clean-freeze", "--exit-zero-on-issues"]
    for key in fix_keys:
        cmd += ["--fix-key", key]
    if apply:
        cmd.append("--fix")
    if any(key in GUARDED_KEYS for key in fix_keys):
        # Human --allow-category path: persist optimization.categoryApproved so
        # later publish/normalize/audit remaps cannot overwrite the leaf.
        cmd.append("--mark-category-approved")
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
    parser.add_argument(
        "--mark-category-approved",
        action="store_true",
        help=(
            "after a successful category remap, stamp categoryApproved + "
            "categoryApprovedId on the stored leaf; categoryApproved-only "
            "rows with no target id stamp the current leaf without an audit"
        ),
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

    return process_rows(
        rows,
        apply=args.apply,
        mark_category_approved=args.mark_category_approved,
        stop_on_error=args.stop_on_error,
        max_consecutive_errors=args.max_consecutive_errors,
    )


def _default_run_audit(sku: str, keys: list[str], apply: bool):
    cmd = build_command(sku, keys, apply)
    # encoding is explicit: the audit prints the store footer (✦) and Chinese
    # summaries, and Windows' default cp936 decode raised inside subprocess's
    # reader thread — the output was lost and every row still reported OK.
    return subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _stamp_or_fail(
    sku: str,
    *,
    apply: bool,
    db_path: Path | None,
    expected_category_id: str = "",
) -> tuple[dict, bool]:
    stamped = stamp_local_category_approved(
        sku,
        apply=apply,
        db_path=db_path,
        expected_category_id=expected_category_id,
    )
    if not stamped.get("ok"):
        print(f"      ! {stamped.get('error')}")
        return stamped, False
    cid = stamped.get("category_id")
    if stamped.get("dry_run"):
        print(f"      would stamp categoryApprovedId={cid} on stored leaf")
    else:
        print(f"      stamped categoryApprovedId={cid} on stored leaf")
    return stamped, True


def process_rows(
    rows: list[dict],
    *,
    apply: bool,
    mark_category_approved: bool,
    stop_on_error: bool = True,
    max_consecutive_errors: int = 3,
    db_path: Path | None = None,
    run_audit=None,
) -> int:
    run_audit = run_audit or _default_run_audit
    failures = 0
    consecutive = 0
    item_errors: list[str] = []
    for index, row in enumerate(rows, start=1):
        sku, keys = row["sku"], list(row["fix_keys"])
        requested = requested_category_id(row)
        print(f"[{index}/{len(rows)}] {sku}  keys={keys}" + (f"  # {row['note']}" if row.get("note") else ""))
        if mark_category_approved and is_stamp_only_row(row):
            stamped, ok = _stamp_or_fail(sku, apply=apply, db_path=db_path)
            if not ok:
                failures += 1
                if stop_on_error:
                    print(f"\n[STOP] {sku}: could not stamp approved category.")
                    return 1
            print()
            continue

        result = run_audit(sku, keys, apply)
        tail = (getattr(result, "stdout", None) or "").strip().splitlines()[-6:]
        for line in tail:
            print(f"      {line}")
        unavailable = _keys_not_offered(sku, keys)
        stampable_missing = [k for k in unavailable if k in STAMPABLE_KEYS]
        other_missing = [k for k in unavailable if k not in STAMPABLE_KEYS]
        if other_missing:
            failures += 1
            print(f"      ! manifest names fix keys the audit does not offer: {other_missing}")
            print(f"      ! nothing was applied for {sku} — fix the manifest, do not retry as-is")
            if stop_on_error:
                print(f"\n[STOP] {sku}: unusable manifest row.")
                return 1
            print()
            continue
        if stampable_missing and not mark_category_approved:
            failures += 1
            print(f"      ! manifest names fix keys the audit does not offer: {stampable_missing}")
            print("      ! category is already correct (or not offered); pass --mark-category-approved to stamp the current leaf")
            if stop_on_error:
                print(f"\n[STOP] {sku}: unusable manifest row.")
                return 1
            print()
            continue

        apply_errors = _apply_errors_for_sku(sku) if apply else []
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
            if stop_on_error and consecutive >= max_consecutive_errors:
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
            err = (getattr(result, "stderr", None) or "").strip().splitlines()[-4:]
            for line in err:
                print(f"      ! {line}")
            if stop_on_error:
                print(f"\n[STOP] {sku} exited {result.returncode}. Nothing further attempted.")
                return 1
            print()
            continue

        if mark_category_approved and any(k in STAMPABLE_KEYS for k in row["fix_keys"]):
            stored = load_stored_category_id(sku, db_path=db_path)
            ok, reason = can_stamp_after_audit(
                returncode=result.returncode,
                apply_errors=apply_errors,
                stored_category_id=stored,
                requested_id=requested,
            )
            if not ok:
                failures += 1
                print(f"      ! {reason}")
                if stop_on_error:
                    print(f"\n[STOP] {sku}: refused to stamp approved category.")
                    return 1
                print()
                continue
            stamped, stamped_ok = _stamp_or_fail(
                sku,
                apply=apply,
                db_path=db_path,
                expected_category_id=requested,
            )
            if not stamped_ok:
                failures += 1
                if stop_on_error:
                    print(f"\n[STOP] {sku}: could not stamp approved category.")
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
