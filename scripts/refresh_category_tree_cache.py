"""Refresh on-disk eBay category trees used by publish path resolution.

Fetches GET /commerce/taxonomy/v1/category_tree/{id} for tree 0 (EBAY_US)
and tree 100 (Motors). Writes data/cache/category_tree_<id>.json.

This is the only supported way to download taxonomy trees. Publish and
dry-run read the cache and never call get_category_tree / get_category_subtree.

Usage:
    python scripts/refresh_category_tree_cache.py
    python scripts/refresh_category_tree_cache.py --trees 0,100
"""

from __future__ import annotations

import argparse
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
)


def _parse_trees(value: str) -> list[str]:
    trees = [part.strip() for part in str(value or "").split(",") if part.strip()]
    return trees or list(DEFAULT_TREE_IDS)


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
    args = parser.parse_args(argv)

    import requests
    from src.services.ebay_auth import EbayOAuthService

    oauth = EbayOAuthService()
    token = oauth.get_application_token()
    api_base = getattr(oauth, "api_base", "https://api.ebay.com")
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
    )
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
