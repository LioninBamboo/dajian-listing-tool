"""Human-approved eBay category lock for publish / normalize / audit.

Operators mark a product's stored category as approved by setting
``optimization.categoryApproved = true`` and pinning
``optimization.categoryApprovedId`` to that leaf (same JSON that holds
``categoryId``). The allow-category path
(``scripts/apply_approved_fixes.py --allow-category --mark-category-approved``)
stamps both fields to an explicit manifest ``category_id``.

While the lock is set, canonicalize / profile-normalize / audit remap
must keep that stored category if it is a sellable leaf in the cached
category tree and is not on the high-insertion-fee blacklist
(``data/config/high_fee_category_blacklist.json``, key
``publish_skip_category_ids``). The fee/blacklist gate always wins.
Unapproved products keep the existing remap behavior.

The lock is bound to ``categoryApprovedId``. If another tool later
rewrites ``categoryId`` without clearing the flag, the new id does not
inherit protection. Legacy rows that only have ``categoryApproved=true``
are still treated as approved for the current ``categoryId``; persist the
id with ``backfill_approved_category_id`` (``is_category_approved`` does
not mutate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from src.utils.category_tree_cache import is_cached_leaf_category
from src.utils.publish_fee_preview import category_is_high_fee


CATEGORY_APPROVED_KEY = "categoryApproved"
CATEGORY_APPROVED_ID_KEY = "categoryApprovedId"

APPROVED_CATEGORY_NON_LEAF_ERROR = (
    "Approved category {category_id} is not a sellable leaf in the cached category tree"
)
APPROVED_CATEGORY_BLACKLIST_ERROR = "high_insertion_fee"

_TRUTHY = {True, 1, "1", "true", "True", "yes", "Yes"}
logger = logging.getLogger(__name__)


def approved_category_id(optimization: Mapping[str, Any] | None) -> str:
    if not isinstance(optimization, Mapping):
        return ""
    return str(optimization.get(CATEGORY_APPROVED_ID_KEY) or "").strip()


def is_category_approved(
    optimization: Mapping[str, Any] | None,
    category_id: str | None = None,
) -> bool:
    if not isinstance(optimization, Mapping):
        return False
    if optimization.get(CATEGORY_APPROVED_KEY) not in _TRUTHY:
        return False

    current = str(category_id or optimization.get("categoryId") or "").strip()
    pinned = approved_category_id(optimization)
    if not pinned:
        # Legacy flag-only rows: honor the current leaf, but do not write.
        return bool(current)

    if pinned != current:
        logger.warning(
            "categoryApprovedId=%s does not match current categoryId=%s; treating as unapproved",
            pinned,
            current,
        )
        return False
    return True


def backfill_approved_category_id(
    optimization: dict[str, Any],
    category_id: str | None = None,
) -> str:
    """Persist ``categoryApprovedId`` for a legacy flag-only row."""
    if not isinstance(optimization, dict):
        return ""
    if optimization.get(CATEGORY_APPROVED_KEY) not in _TRUTHY:
        return ""
    pinned = approved_category_id(optimization)
    if pinned:
        return pinned
    cid = str(category_id or optimization.get("categoryId") or "").strip()
    if not cid:
        return ""
    optimization[CATEGORY_APPROVED_ID_KEY] = cid
    logger.info("Backfilled categoryApprovedId=%s from current categoryId", cid)
    return cid


def mark_category_approved(
    optimization: dict[str, Any],
    category_id: str | None = None,
) -> dict[str, Any]:
    cid = str(category_id or optimization.get("categoryId") or "").strip()
    optimization[CATEGORY_APPROVED_KEY] = True
    if cid:
        optimization[CATEGORY_APPROVED_ID_KEY] = cid
    return optimization


def approved_category_non_leaf_error(category_id: str) -> str:
    return APPROVED_CATEGORY_NON_LEAF_ERROR.format(category_id=category_id)


@dataclass(frozen=True)
class ApprovedCategoryDecision:
    """Outcome of the approved-category gate.

    ``preserve``: skip canonicalize / profile / audit remaps and keep the id.
    ``error``: publish/audit blocker when the approval cannot be honored.
    """

    approved: bool
    preserve: bool
    error: Optional[str] = None


def evaluate_approved_category(
    category_id: str,
    optimization: Mapping[str, Any] | None = None,
    *,
    blacklist_path: Optional[str] = None,
    cache_dir: Optional[Path] = None,
) -> ApprovedCategoryDecision:
    if not is_category_approved(optimization, category_id):
        return ApprovedCategoryDecision(approved=False, preserve=False)

    cid = str(category_id or "").strip()
    if category_is_high_fee(cid, blacklist_path=blacklist_path):
        return ApprovedCategoryDecision(
            approved=True,
            preserve=False,
            error=APPROVED_CATEGORY_BLACKLIST_ERROR,
        )
    if not is_cached_leaf_category(cid, cache_dir=cache_dir):
        return ApprovedCategoryDecision(
            approved=True,
            preserve=False,
            error=approved_category_non_leaf_error(cid),
        )
    return ApprovedCategoryDecision(approved=True, preserve=True)
