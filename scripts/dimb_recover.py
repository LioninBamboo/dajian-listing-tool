#!/usr/bin/env python3
"""Dimension-B recovery for single-item products missing assembled dims.

find_dimension() only recognizes Assembled/Overall/Product keys, not Package.
For genuine single-item goods (tools, panels — no flat-pack assembly) the package
box is the product, so copy Package L/W/H (in.) -> Assembled L/W/H (in.) and
Package Weight -> Product Weight when the assembled fields are empty. Then reset
to COLLECTED so batch_analyze re-optimizes with dims present.

    python scripts/dimb_recover.py --skus-file <json-list>
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.db.collection_db import SessionLocal
from src.db.collection_models import CollectedProduct
from sqlalchemy.orm.attributes import flag_modified

PAIRS = [
    ("Package Length (in.)", "Assembled Length (in.)"),
    ("Package Width (in.)", "Assembled Width (in.)"),
    ("Package Height (in.)", "Assembled Height (in.)"),
    ("Package Weight (lbs.)", "Product Weight (lbs.)"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skus-file", required=True)
    args = ap.parse_args()
    skus = json.load(open(args.skus_file, encoding="utf-8"))
    db = SessionLocal()
    done = 0
    for sku in skus:
        row = db.query(CollectedProduct).filter_by(sku=sku).first()
        if not row:
            print(f"  {sku}: not found"); continue
        attrs = dict(row.attributes or {})
        specs = dict(row.specs or {})
        src = {**specs, **attrs}
        changed = []
        for pk, ak in PAIRS:
            if not attrs.get(ak) and src.get(pk):
                attrs[ak] = str(src[pk]); changed.append(ak.split()[1][0])
        if not changed:
            print(f"  {sku}: no package dims to copy, skip"); continue
        row.attributes = attrs
        flag_modified(row, "attributes")
        row.status = "COLLECTED"
        db.commit()
        done += 1
        print(f"  {sku}: filled {''.join(changed)} -> COLLECTED")
    db.close()
    print(f"done: {done} recovered")


if __name__ == "__main__":
    main()
