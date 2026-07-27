#!/usr/bin/env python
"""Check semantic-rewrite preview markdown for defects in the AFTER copy only.

Written after a hand-rolled `grep -l "adds practical seating" *.md` check gave a
false alarm on 2026-07-27: preview files carry BOTH the current live copy
(### Before) and the proposed copy (### After), so grepping the whole file
always matches the very defect the rewrite is there to remove. Every rule here
reads the AFTER block exclusively.

Exit 0 = clean, 1 = defects found (the count is the report, not a crash).

    python scripts/verify_rewrite_preview.py reports/crit_backlog_preview_v2
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

AFTER_RE = re.compile(r"### After\n```\n(.*?)\n```", re.S)
BEFORE_RE = re.compile(r"### Before\n```\n(.*?)\n```", re.S)
PUSHABLE_RE = re.compile(r"^- \*\*pushable:\*\* (True|False)", re.M)

# Fabricated benefits: the old PERFECT FOR fallback asserted these for every
# product type, including a circular saw and a push reel lawn mower.
FABRICATION_MARKERS = (
    "adds practical seating",
    "everyday comfort",
)
GLUED_HEADING_MARKERS = (
    "PERFECT FOR Product Features",
    "PERFECT FOR Features",
    "PERFECT FOR Specifications",
)
REQUIRED_TEMPLATE_MARKERS = (
    "AQUAVERVE",
    "KEY FEATURES",
    "Ships from",
)


def _section(text: str, pattern: re.Pattern) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def check_one(path: Path) -> tuple[list[str], list[str]]:
    """Return (blocking, warnings) defect lists for one preview file.

    Severity is split deliberately. A claim the source does not support is a
    refund risk and must stop the batch. Redundant-but-true copy is a polish
    issue: reported, never blocking. Gating on cosmetics is what produced the
    2026-07-27 false alarm that halted a batch which was in fact correct.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    blocking: list[str] = []
    warnings: list[str] = []

    verdicts = PUSHABLE_RE.findall(text)
    if verdicts and verdicts[0] != "True":
        return [], []  # not a push candidate; nothing to verify

    after = _section(text, AFTER_RE)
    if not after:
        # A skip report (source unavailable) legitimately has no AFTER block.
        if "— SKIP" not in text:
            blocking.append("no AFTER block found")
        return blocking, warnings

    lowered = after.lower()
    for marker in FABRICATION_MARKERS:
        if marker in lowered:
            blocking.append(f"fabricated benefit: {marker!r}")
    for marker in GLUED_HEADING_MARKERS:
        if marker.lower() in lowered:
            blocking.append(f"glued section heading: {marker!r}")
    for marker in REQUIRED_TEMPLATE_MARKERS:
        if marker.lower() not in lowered:
            blocking.append(f"template marker missing: {marker!r}")

    if any("一" <= ch <= "鿿" for ch in after):
        blocking.append("Chinese characters in AFTER copy")

    # True but redundant: the same sentence under both headings. Source HTML
    # that omits a full stop glues clauses together, so a residual few are
    # supplier-data artifacts rather than pipeline bugs.
    if "PERFECT FOR" in after and "KEY FEATURES" in after:
        features = after.split("KEY FEATURES", 1)[1].split("PERFECT FOR")[0]
        perfect_for = after.split("PERFECT FOR", 1)[1].strip()
        head = perfect_for[:70].strip()
        if head and head in features:
            warnings.append("PERFECT FOR duplicates a KEY FEATURES bullet")

    return blocking, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preview_dir", help="directory of *.md preview reports")
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args()

    directory = Path(args.preview_dir)
    if not directory.is_dir():
        print(f"[ERROR] not a directory: {directory}")
        return 2

    files = sorted(directory.glob("*.md"))
    if not files:
        print(f"[ERROR] no preview files in {directory}")
        return 2

    n_blocking = 0
    n_warnings = 0
    files_blocked = 0
    files_warned = 0
    for path in files:
        blocking, warnings = check_one(path)
        if blocking:
            files_blocked += 1
            n_blocking += len(blocking)
        if warnings:
            files_warned += 1
            n_warnings += len(warnings)
        if (blocking or warnings) and not args.quiet:
            print(f"{path.name}:")
            for defect in blocking:
                print(f"    [BLOCK] {defect}")
            for defect in warnings:
                print(f"    [warn ] {defect}")

    print()
    print(
        f"checked {len(files)} preview(s): "
        f"{files_blocked} blocking ({n_blocking} defect(s)), "
        f"{files_warned} with warnings ({n_warnings})"
    )
    if n_blocking:
        print("RESULT: BLOCKED — unsupported claims present. Stop and report; do not push.")
        return 1
    if n_warnings:
        print("RESULT: OK WITH WARNINGS — no unsupported claims. Warnings are cosmetic; safe to push.")
        return 0
    print("RESULT: CLEAN — safe to proceed to canary.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
