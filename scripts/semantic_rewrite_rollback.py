#!/usr/bin/env python3
"""Rollback a semantic rewrite from logs/semantic_rewrite_backups/{sku}.json.

Dual-gated: requires --apply AND SEMANTIC_REWRITE_APPLY_ENABLED=1.
P0 only needs unit-test coverage with mocks — do not live-run in P0.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from src.services.semantic_rewrite import (  # noqa: E402
    APPLY_ENV_FLAG,
    BACKUP_DIR,
    dual_gate_allows_apply,
    dual_gate_block_reason,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Rollback semantic rewrite from backup snapshot")
    p.add_argument("--sku", required=True, help="SKU to restore")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually restore (requires SEMANTIC_REWRITE_APPLY_ENABLED=1)",
    )
    p.add_argument(
        "--backup-dir",
        default=str(BACKUP_DIR),
        help="Directory containing {sku}.json backups",
    )
    return p


def load_backup(sku: str, backup_dir: Path) -> dict:
    path = backup_dir / f"{sku}.json"
    if not path.is_file():
        raise FileNotFoundError(f"backup not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def rollback_sku(
    ebay,
    conn,
    sku: str,
    *,
    cli_apply: bool,
    backup_dir: Path = BACKUP_DIR,
) -> dict:
    if not dual_gate_allows_apply(cli_apply=cli_apply):
        return {
            "ok": False,
            "sku": sku,
            "reason": dual_gate_block_reason(cli_apply=cli_apply),
        }

    before = load_backup(sku, backup_dir)
    title = before.get("title") or ""
    description = before.get("description") or ""
    aspects = dict(before.get("aspects") or {})
    offer_id = before.get("offer_id")
    category_id = before.get("category_id")
    listing_id = before.get("listing_id")

    # Gap 2/3: restore must re-fill category-required aspects (backup may predate a
    # required field eBay now enforces after a partial rewrite).
    from src.services.semantic_rewrite import protect_and_fill_required_aspects
    from scripts.audit_fix_active_listings import _put_inventory_product_only

    aspects, _, _ = protect_and_fill_required_aspects(
        aspects,
        category_id=str(category_id or ""),
        title=title,
        source_attrs={},
        source_sheet=None,
        before_aspects=before.get("aspects") or {},
    )

    _put_inventory_product_only(ebay, sku, title, description, aspects)

    if offer_id and hasattr(ebay, "update_offer_category"):
        ebay.update_offer_category(
            offer_id,
            str(category_id or ""),
            listing_description=description,
        )
    if offer_id and hasattr(ebay, "publish_offer"):
        pub = ebay.publish_offer(offer_id) or {}
        listing_id = pub.get("listingId") or listing_id

    row = conn.execute(
        "SELECT optimization, logs FROM collected_products WHERE sku = ?", (sku,)
    ).fetchone()
    if row:
        opt_raw, logs_raw = row[0], row[1]
        try:
            opt = json.loads(opt_raw) if isinstance(opt_raw, str) else dict(opt_raw or {})
        except Exception:
            opt = {}
        opt["title"] = title
        opt["description"] = description
        opt["aspects"] = aspects
        if category_id:
            opt["categoryId"] = str(category_id)
        try:
            logs = json.loads(logs_raw) if isinstance(logs_raw, str) else list(logs_raw or [])
        except Exception:
            logs = []
        logs.append({"event": "semantic_rewrite_rollback", "sku": sku})
        conn.execute(
            "UPDATE collected_products SET optimization = ?, logs = ? WHERE sku = ?",
            (json.dumps(opt, ensure_ascii=False), json.dumps(logs, ensure_ascii=False), sku),
        )
        conn.commit()

    return {
        "ok": True,
        "sku": sku,
        "reason": "rolled_back",
        "listing_id": listing_id,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and not dual_gate_allows_apply(cli_apply=True):
        print(f"ERROR: {dual_gate_block_reason(cli_apply=True)}")
        return 3

    if not args.apply:
        # dry preview of backup contents
        try:
            data = load_backup(args.sku, Path(args.backup_dir))
        except FileNotFoundError as e:
            print(e)
            return 2
        print(f"Would restore {args.sku} from backup:")
        print(f"  title: {data.get('title')}")
        print(f"  offer_id: {data.get('offer_id')} listing_id: {data.get('listing_id')}")
        print(f"  aspects keys: {list((data.get('aspects') or {}).keys())[:12]}")
        print(f"(pass --apply and {APPLY_ENV_FLAG}=1 to execute)")
        return 0

    from src.clients.real_ebay_client import create_real_ebay_client

    env = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper()
    ebay = create_real_ebay_client(env)
    conn = sqlite3.connect(str(ROOT / "ebay_collection.db"))
    result = rollback_sku(
        ebay,
        conn,
        args.sku,
        cli_apply=True,
        backup_dir=Path(args.backup_dir),
    )
    conn.close()
    print(result)
    return 0 if result.get("ok") else 4


if __name__ == "__main__":
    raise SystemExit(main())
