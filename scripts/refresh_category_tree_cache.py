"""Refresh on-disk eBay category trees used by publish path resolution.

Fetches GET /commerce/taxonomy/v1/category_tree/{id} for tree 0 (EBAY_US)
and tree 100 (Motors). Writes data/cache/category_tree_<id>.json.

Uses production taxonomy by default (same as the server and other prod
scripts). Refuses to write if oauth.api_base is not https://api.ebay.com
unless --allow-sandbox is passed.

This is the only supported way to download taxonomy trees. Publish and
dry-run read the cache and never call get_category_tree / get_category_subtree.

Usage:
    python scripts/refresh_category_tree_cache.py
    python scripts/refresh_category_tree_cache.py --trees 0,100
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.category_tree_cache import (
    DEFAULT_CACHE_DIR,
    DEFAULT_TREE_IDS,
    fetch_category_tree,
    refresh_category_tree_cache,
    require_production_api_base,
)


def _parse_trees(value: str) -> list[str]:
    trees = [part.strip() for part in str(value or "").split(",") if part.strip()]
    return trees or list(DEFAULT_TREE_IDS)


def refresh_oauth_environment() -> str:
    return os.getenv("EBAY_ENVIRONMENT", "PRODUCTION").upper()


def build_refresh_oauth():
    from src.services.ebay_auth import EbayOAuthService

    return EbayOAuthService(refresh_oauth_environment())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trees",
        default=",".join(DEFAULT_TREE_IDS),
        help="Comma-separated taxonomy tree ids (default: 0,100)",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Directory for category_tree_<id>.json snapshots",
    )
    parser.add_argument(
        "--allow-sandbox",
        action="store_true",
        help="Allow writing a snapshot from api.sandbox.ebay.com",
    )
    args = parser.parse_args(argv)

    import requests

    oauth = build_refresh_oauth()
    api_base = getattr(oauth, "api_base", "")
    try:
        source_host = require_production_api_base(
            api_base,
            allow_sandbox=args.allow_sandbox,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    token = oauth.get_application_token()
    session = requests.Session()

    def _fetch(tree_id: str):
        return fetch_category_tree(
            tree_id,
            session=session,
            token=token,
            api_base=api_base,
        )

    written = refresh_category_tree_cache(
        _parse_trees(args.trees),
        fetch=_fetch,
        cache_dir=Path(args.cache_dir),
        source_host=source_host,
        allow_sandbox=args.allow_sandbox,
    )
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
