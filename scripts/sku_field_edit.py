#!/usr/bin/env python3
"""Thin CLI for the single-SKU field editor. Parser imports no DB/OAuth/network."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _OpAction(argparse.Action):
    """Append edit ops in the order they appear on the command line."""

    def __call__(self, parser, namespace, values, option_string=None):
        ops = getattr(namespace, "cli_ops", None)
        if ops is None:
            ops = []
            namespace.cli_ops = ops
        flag = option_string or ""
        if flag == "--strip-claim":
            ops.append({"kind": "strip_claim", "text": values, "location": ""})
            return
        if flag == "--location":
            if not ops or ops[-1].get("kind") != "strip_claim":
                parser.error("--location must follow --strip-claim")
            ops[-1]["location"] = values
            return
        if flag == "--remove-aspect-value":
            ops.append({"kind": "remove_aspect_value", "spec": values})
            return
        if flag == "--remove-aspect":
            ops.append({"kind": "remove_aspect", "name": values})
            return
        if flag == "--set-from-file":
            ops.append({"kind": "set_from_file", "spec": values})
            return
        if flag == "--set-category":
            ops.append({"kind": "set_category", "category_id": values})
            return
        parser.error(f"unknown edit flag {flag}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sku_field_edit.py",
        description=(
            "Dry-run-first editor for one SKU (or an explicit list). "
            "Default is dry-run: no DB or eBay writes."
        ),
    )
    parser.add_argument("--store", required=True, choices=("main", "gp", "aqua"))
    sku_group = parser.add_mutually_exclusive_group(required=True)
    sku_group.add_argument("--sku")
    sku_group.add_argument("--skus", help="Comma-separated SKUs, max 50")
    sku_group.add_argument("--skus-file", dest="skus_file")
    parser.add_argument("--strip-claim", action=_OpAction)
    parser.add_argument(
        "--location",
        action=_OpAction,
        help="description | features | title | all | aspect:<Name>",
    )
    parser.add_argument(
        "--remove-aspect-value",
        action=_OpAction,
        help='Name=Value',
    )
    parser.add_argument("--remove-aspect", action=_OpAction)
    parser.add_argument("--set-from-file", action=_OpAction, help="FIELD=PATH")
    parser.add_argument("--set-category", action=_OpAction)
    parser.add_argument("--manifest", help="JSON list of per-SKU ops; cannot mix with CLI ops")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--push-live", action="store_true")
    parser.add_argument("--allow-category", action="store_true")
    parser.add_argument(
        "--allow-multi-unit",
        action="store_true",
        help="Required with --apply when one --strip-claim removes more than one bullet/paragraph.",
    )
    parser.add_argument("--override-category-lock", action="store_true")
    parser.add_argument("--mark-category-approved", action="store_true")
    parser.add_argument("--backup-dir")
    parser.add_argument("--report")
    parser.add_argument("--note", default="")
    parser.add_argument(
        "--stop-on-error",
        dest="stop_on_error",
        action="store_true",
        default=True,
    )
    parser.add_argument("--no-stop-on-error", dest="stop_on_error", action="store_false")
    parser.set_defaults(cli_ops=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    from src.services.sku_field_edit import run_from_args

    return run_from_args(args)


if __name__ == "__main__":
    sys.exit(main())
