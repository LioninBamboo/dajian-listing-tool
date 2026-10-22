"""eBay Motors category tree 100 leaf check.

AquaRides publishes Motors parts through the Trading API (SiteID 100). Those
listings may only use a leaf from category tree 100. Tree-0 furniture ids such
as 20487 (Cabinets & Cupboards) are absent from that tree and come back from
eBay as ``invalid category``. Some numbers exist in both trees with different
names (174020 is Trailer Hitches in tree 0 and Brake Pad Wear Sensors in tree
100), so a tree-0 keyword fallback must never be substituted just because the
number happens to be present.

The leaf set is a local snapshot (``data/motors_category_tree_100_leaves.json``)
so publish decisions do not call eBay. An id that is not in the snapshot is
rejected. Callers block the listing; they do not pick a different id.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_LEAF_PATH = Path(__file__).resolve().parents[2] / "data" / "motors_category_tree_100_leaves.json"

NOT_TREE100_LEAF_ERROR = (
    "category {category_id} is not a leaf in eBay Motors category tree 100; "
    "refusing to fall back to a tree-0 id"
)


@lru_cache(maxsize=1)
def tree100_leaf_ids() -> frozenset[str]:
    raw = json.loads(_LEAF_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(str(item).strip() for item in raw if str(item).strip())


def is_tree100_leaf(category_id: str | None) -> bool:
    cid = str(category_id or "").strip()
    return bool(cid) and cid.isdigit() and cid in tree100_leaf_ids()


def motors_category_rejection(category_id: str | None) -> str:
    """Empty when ``category_id`` may be used on a Motors (tree 100) listing."""
    cid = str(category_id or "").strip()
    if not cid:
        return "missing categoryId"
    if not cid.isdigit() or not is_tree100_leaf(cid):
        return NOT_TREE100_LEAF_ERROR.format(category_id=cid or "(blank)")
    return ""


def listing_category_rejection(category_id: str | None) -> str:
    """Reject non-tree-100 ids on Motors. Furniture (tree 0) is unchanged."""
    try:
        from src.utils.store_profile import get_store_profile

        if not get_store_profile().is_motors:
            return ""
    except Exception:
        return ""
    return motors_category_rejection(category_id)
