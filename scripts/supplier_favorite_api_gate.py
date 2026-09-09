#!/usr/bin/env python3
"""Favorite / API readability gate for single-supplier batch listing.

DaJian stock/price/detail APIs only work for SKUs in the buyer favorites
(or domestic warehouse). Plugin collect auto-favorites; page-only collect does
not. Run this gate BEFORE collect/publish so blocked SKUs never enter the queue.

Usage:
  python scripts/supplier_favorite_api_gate.py --sku-list tools/w714_listing/B4_skus.txt
  python scripts/supplier_favorite_api_gate.py --skus W714S01942,W714S01943 --json-out report.json
  python scripts/supplier_favorite_api_gate.py --sku-list list.txt --require-all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.clients.dajian_client import DaJianClient
from src.utils.ebay_quantity import fetch_live_supplier_quantity


def load_skus(args: argparse.Namespace) -> list[str]:
    skus: list[str] = []
    if args.skus:
        skus.extend(s.strip() for s in args.skus.split(",") if s.strip())
    if args.sku_list:
        path = Path(args.sku_list)
        skus.extend(
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    # preserve order, drop dupes
    seen: set[str] = set()
    out: list[str] = []
    for sku in skus:
        if sku not in seen:
            seen.add(sku)
            out.append(sku)
    return out


def check_sku(sku: str, client: DaJianClient) -> dict:
    row: dict = {"sku": sku, "api_readable": False, "quantity": None, "error": None}
    try:
        info = client.get_stock_info(sku)
        qty = info.get("quantity")
        try:
            qty_i = max(0, int(qty)) if qty is not None else None
        except (TypeError, ValueError):
            qty_i = None
        row["quantity"] = qty_i
        row["api_readable"] = True
        row["in_stock"] = bool(info.get("in_stock")) if "in_stock" in info else (qty_i or 0) > 0
        row["price"] = info.get("price")
        row["shipping_fee"] = info.get("shipping_fee")
    except Exception as exc:
        # fallback through shared helper (same client path)
        try:
            qty = fetch_live_supplier_quantity(sku, dajian_client=client)
            if qty is not None:
                row["quantity"] = qty
                row["api_readable"] = True
                row["in_stock"] = qty > 0
            else:
                row["error"] = str(exc)[:240]
        except Exception as exc2:
            row["error"] = str(exc2)[:240] or str(exc)[:240]
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate: SKUs must be API-readable (favorited)")
    ap.add_argument("--sku-list", type=Path, help="Text file, one SKU per line")
    ap.add_argument("--skus", help="Comma-separated SKUs")
    ap.add_argument("--json-out", type=Path, help="Write full report JSON")
    ap.add_argument(
        "--require-all",
        action="store_true",
        help="Exit 2 if any SKU is not API-readable (hard gate)",
    )
    ap.add_argument("--sleep", type=float, default=0.15, help="Delay between API calls")
    args = ap.parse_args()

    skus = load_skus(args)
    if not skus:
        print("ERROR: no SKUs provided", file=sys.stderr)
        return 1

    import os

    key = os.getenv("DAJIAN_API_KEY")
    secret = os.getenv("DAJIAN_API_SECRET")
    if not key or not secret:
        print("ERROR: missing DAJIAN_API_KEY / DAJIAN_API_SECRET", file=sys.stderr)
        return 1
    client = DaJianClient(key, secret)

    rows = []
    for i, sku in enumerate(skus, 1):
        row = check_sku(sku, client)
        rows.append(row)
        mark = "OK" if row["api_readable"] else "BLOCKED"
        extra = (
            f"qty={row['quantity']}"
            if row["api_readable"]
            else (row.get("error") or "not readable")[:100]
        )
        print(f"[{i}/{len(skus)}] {mark} {sku} {extra}", flush=True)
        if args.sleep:
            time.sleep(args.sleep)

    ok = [r for r in rows if r["api_readable"]]
    blocked = [r for r in rows if not r["api_readable"]]
    report = {
        "total": len(rows),
        "api_readable": len(ok),
        "blocked": len(blocked),
        "blocked_skus": [r["sku"] for r in blocked],
        "readable_skus": [r["sku"] for r in ok],
        "rows": rows,
        "hint": (
            "Blocked SKUs are not in DaJian buyer favorites (or API denied). "
            "Open each on GIGA with the browser extension and click the heart, "
            "then re-run this gate before collect/publish."
        ),
    }

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("wrote", args.json_out, flush=True)

    print(
        f"SUMMARY readable={len(ok)}/{len(rows)} blocked={len(blocked)}",
        flush=True,
    )
    if blocked:
        print("BLOCKED:", ", ".join(r["sku"] for r in blocked), flush=True)
        print(report["hint"], flush=True)

    if args.require_all and blocked:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
