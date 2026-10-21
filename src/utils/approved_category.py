"""Human-approved eBay category lock for publish / normalize / audit.

Operators mark a product's stored category as approved by setting
``optimization.categoryApproved = true`` (same JSON that holds
``categoryId``). The allow-category path
(``scripts/apply_approved_fixes.py --allow-category``) stamps this flag
when it applies a ``categoryId`` / ``categoryName`` fix.

While the flag is set, canonicalize / profile-normalize / audit remap
must keep the stored category if it is a sellable leaf in the cached
category tree and is not on the high-insertion-fee blacklist
(``data/config/high_fee_category_blacklist.json``, key
``publish_skip_category_ids``). The fee/blacklist gate always wins.
Unapproved products keep the existing remap behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from src.utils.category_tree_cache import is_cached_leaf_category
from src.utils.publish_fee_preview import category_is_high_fee


CATEGORY_APPROVED_KEY = "categoryApproved"

APPROVED_CATEGORY_NON_LEAF_ERROR = (
    "Approved category {category_id} is not a sellable leaf in the cached category tree"
)
APPROVED_CATEGORY_BLACKLIST_ERROR = "high_insertion_fee"

_TRUTHY = {True, 1, "1", "true", "True", "yes", "Yes"}


def is_category_approved(optimization: Mapping[str, Any] | None) -> bool:
    if not isinstance(optimization, Mapping):
        return False
    return optimization.get(CATEGORY_APPROVED_KEY) in _TRUTHY


def mark_category_approved(optimization: dict[str, Any]) -> dict[str, Any]:
    optimization[CATEGORY_APPROVED_KEY] = True
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
    if not is_category_approved(optimization):
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
