"""
One-off Main DB remap for retired eBay categories 32880 and 42430.

Does not run unless you pass --apply. Does not invent item specifics.
Aspects are left untouched; publish refetches required aspects for the new leaf.

Backup first (also done automatically on --apply):
  backups/ebay_collection_pre_catfix_20261016.db

Primary SKUs (applied by --apply when the stored categoryId still matches `old`):
  W2987P351728  32880 → 22656  Coat & Hat Racks
  W2987P382195  32880 → 22656  Coat & Hat Racks
  W540133888    42430 → 161059 Trash Can Storage
      Evidence: suggestion #2, leaf. #1 was 20608 Trash Cans & Wastebaskets
      (the bin, not the shed). 139956 Sheds is the alternate only.

Recommended extras — Sergey to confirm. This script does NOT write them.
  W40939293     wall coat rack → 22656 Coat & Hat Racks
  W465P408007   bolt cutter → re-suggest via Taxonomy API (do not hardcode an ID)
  W465121328    garage tire rack → re-suggest via Taxonomy API (do not hardcode an ID)

After --apply, sweep:
  select sku, status from collected_products
  where json_extract(optimization, '$.categoryId') in ('32880', '42430', '3197');
Expect 0 rows on Main. Repeat on GrovePop and AquaRides DBs (not part of this PR).

Prices to confirm on the dry-run publish (do not change them here):
  W2987P351728 81.61
  W2987P382195 81.61
  W540133888   192.63
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

LOG_LINE = "category_fix_20261016 {old}→{new}"

# Applied only with --apply. Names are the leaf names from the 2026-10-16 spec.
PRIMARY_SKU_REMAPS = (
    {
        "sku": "W2987P351728",
        "old": "32880",
        "new": "22656",
        "name": "Coat & Hat Racks",
    },
    {
        "sku": "W2987P382195",
        "old": "32880",
        "new": "22656",
        "name": "Coat & Hat Racks",
    },
    {
        "sku": "W540133888",
        "old": "42430",
        "new": "161059",
        "name": "Trash Can Storage",
    },
)

# Not written. Listed so the operator sees them next to the primary fix.
EXTRA_NOTES = (
    "W40939293 wall coat rack → 22656 Coat & Hat Racks (confirm, then remap)",
    "W465P408007 bolt cutter → re-suggest via API; do not invent a category or item specifics",
    "W465121328 garage tire rack → re-suggest via API; do not invent a category or item specifics",
    "Bin-shed alternate if 161059 is rejected in review: 139956 Sheds. Default remains 161059.",
)


def _load_json(value):
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    return json.loads(value)


def plan(conn: sqlite3.Connection):
    actions = []
    for row in PRIMARY_SKU_REMAPS:
        found = conn.execute(
            "SELECT sku, status, optimization, logs FROM collected_products WHERE sku = ?",
            (row["sku"],),
        ).fetchone()
        if found is None:
            actions.append({**row, "action": "missing"})
            continue
        opt = _load_json(found["optimization"])
        current = str(opt.get("categoryId") or "")
        if current == row["new"]:
            actions.append({**row, "action": "already", "status": found["status"]})
        elif current != row["old"]:
            actions.append({**row, "action": "skip", "status": found["status"], "current": current})
        else:
            actions.append({**row, "action": "update", "status": found["status"]})
    return actions


def apply_updates(conn: sqlite3.Connection, actions) -> int:
    changed = 0
    for row in actions:
        if row["action"] != "update":
            continue
        found = conn.execute(
            "SELECT optimization, logs FROM collected_products WHERE sku = ?",
            (row["sku"],),
        ).fetchone()
        opt = _load_json(found["optimization"])
        logs = _load_json(found["logs"])
        if not isinstance(logs, list):
            logs = []
        # Category fields only. aspects / price / status stay as they are.
        opt["categoryId"] = row["new"]
        opt["categoryName"] = row["name"]
        logs.append(LOG_LINE.format(old=row["old"], new=row["new"]))
        conn.execute(
            """
            UPDATE collected_products
            SET optimization = ?, logs = ?, updated_at = datetime('now')
            WHERE sku = ?
            """,
            (json.dumps(opt, ensure_ascii=False), json.dumps(logs, ensure_ascii=False), row["sku"]),
        )
        changed += 1
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to a COPY of ebay_collection.db")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the three primary SKUs. Default is dry-run.",
    )
    args = parser.parse_args(argv)
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 2

    print("Recommended extras (not modified):")
    for note in EXTRA_NOTES:
        print(f"  - {note}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if not args.apply else None
    if args.apply:
        backup = db_path.parent / "backups" / "ebay_collection_pre_catfix_20261016.db"
        if db_path.parent.name == "backups":
            backup = db_path.with_name(db_path.stem + "_pre_catfix_20261016.db")
        else:
            backup = db_path.parent / "backups" / "ebay_collection_pre_catfix_20261016.db"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup)
        print(f"backup: {backup}")
        conn = sqlite3.connect(db_path)

    conn.row_factory = sqlite3.Row
    actions = plan(conn)
    for row in actions:
        print(f"{row['sku']}: {row['action']} {row['old']}→{row['new']} {row['name']}")
    if not args.apply:
        print("dry-run only. Re-run with --apply on a backed-up DB to write.")
        conn.close()
        return 0
    changed = apply_updates(conn, actions)
    conn.commit()
    conn.close()
    print(f"updated {changed} row(s). Item specifics were not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
