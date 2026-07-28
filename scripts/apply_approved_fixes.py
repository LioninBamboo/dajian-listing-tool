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
    cmd = [sys.executable, str(AUDIT), "--sku", sku, "--live", "--ignore-clean-freeze"]
    for key in fix_keys:
        cmd += ["--fix-key", key]
    if apply:
        cmd.append("--fix")
    return cmd


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
    args = parser.parse_args()

    rows = load_manifest(Path(args.manifest))
    check_guarded(rows, args.allow_category)

    print(f"manifest: {args.manifest}")
    print(f"rows    : {len(rows)}")
    print(f"mode    : {'APPLY (writes to eBay)' if args.apply else 'DRY RUN'}")
    print()

    failures = 0
    for index, row in enumerate(rows, start=1):
        sku, keys = row["sku"], row["fix_keys"]
        print(f"[{index}/{len(rows)}] {sku}  keys={keys}" + (f"  # {row['note']}" if row["note"] else ""))
        cmd = build_command(sku, keys, args.apply)
        result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        tail = (result.stdout or "").strip().splitlines()[-6:]
        for line in tail:
            print(f"      {line}")
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
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
