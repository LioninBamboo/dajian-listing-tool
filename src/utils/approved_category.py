"""Human-approved eBay category lock for publish / normalize / audit.

Operators mark a product's stored category as approved by setting
``optimization.categoryApproved = true`` and pinning
``optimization.categoryApprovedId`` to that leaf (same JSON that holds
``categoryId``). The allow-category path
(``scripts/apply_approved_fixes.py --allow-category --mark-category-approved``)
stamps both fields to an explicit manifest ``category_id``.

While the lock is set, canonicalize / profile-normalize / audit remap
must keep that stored category. Publish may honor the lock only when the
id is a well-formed sellable leaf and is not on the high-insertion-fee
blacklist. The fee/blacklist gate always wins. Unapproved products keep
the existing remap behavior.

The lock is bound to ``categoryApprovedId``. If another tool later
rewrites ``categoryId`` without clearing the flag, the new id does not
inherit *preservation* (publish will not treat the rewritten leaf as
approved). Automated remaps still fail closed whenever the approval
*flag* is set — including blank / invalid pins — unless a human passes
``--override-category-lock``.

Legacy rows that only have ``categoryApproved=true`` are still treated as
approved for the current ``categoryId``; persist the id with
``backfill_approved_category_id`` (``is_category_approved`` does not
mutate). Existing GrovePop rows that lack both fields are unlocked.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

CATEGORY_APPROVED_KEY = "categoryApproved"
CATEGORY_APPROVED_ID_KEY = "categoryApprovedId"

APPROVED_CATEGORY_NON_LEAF_ERROR = (
    "Approved category {category_id} is not a sellable leaf in the cached category tree"
)
APPROVED_CATEGORY_BLACKLIST_ERROR = "high_insertion_fee"
APPROVED_CATEGORY_INVALID_ID_ERROR = "invalid_or_blank_category_id"

_TRUTHY = {True, 1, "1", "true", "True", "yes", "Yes"}
logger = logging.getLogger(__name__)

_DEFAULT_BLACKLIST = Path("data/config/high_fee_category_blacklist.json")


def is_valid_category_id(category_id: str | None) -> bool:
    cid = str(category_id or "").strip()
    return bool(cid) and cid.isdigit()


def approved_category_id(optimization: Mapping[str, Any] | None) -> str:
    if not isinstance(optimization, Mapping):
        return ""
    return str(optimization.get(CATEGORY_APPROVED_ID_KEY) or "").strip()


def category_approval_flag_set(optimization: Mapping[str, Any] | None) -> bool:
    """True when an operator stamped the approval flag, even if the pin is bad."""
    if not isinstance(optimization, Mapping):
        return False
    return optimization.get(CATEGORY_APPROVED_KEY) in _TRUTHY


def is_category_approved(
    optimization: Mapping[str, Any] | None,
    category_id: str | None = None,
) -> bool:
    if not category_approval_flag_set(optimization):
        return False

    current = str(category_id or (optimization or {}).get("categoryId") or "").strip()
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


def blocks_automated_category_change(
    optimization: Mapping[str, Any] | None,
    *,
    override: bool = False,
) -> bool:
    """Fail-closed remap gate.

    Any truthy ``categoryApproved`` flag blocks automation from moving the
    category, including blank / invalid / mismatched pins. Manual remap
    tools must pass ``override=True`` (``--override-category-lock``).
    """
    if override:
        return False
    return category_approval_flag_set(optimization)


def locked_category_write_error(
    optimization: Mapping[str, Any] | None,
    *,
    override: bool = False,
) -> str:
    if not blocks_automated_category_change(optimization, override=override):
        return ""
    pinned = approved_category_id(optimization) or "(blank)"
    return (
        f"category lock is set (categoryApprovedId={pinned}); "
        "pass --override-category-lock to change the category"
    )


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


def _category_is_high_fee(category_id: str, blacklist_path: Optional[str] = None) -> bool:
    cid = str(category_id or "").strip()
    if not cid:
        return False
    try:
        from src.utils.publish_fee_preview import category_is_high_fee

        return bool(category_is_high_fee(cid, blacklist_path=blacklist_path))
    except ImportError:
        pass
    path = Path(
        blacklist_path
        or os.getenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH")
        or _DEFAULT_BLACKLIST
    )
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    skip_ids = data.get("publish_skip_category_ids") if isinstance(data, dict) else None
    if not isinstance(skip_ids, (list, tuple, set)):
        return False
    return cid in {str(item).strip() for item in skip_ids}


def _is_sellable_leaf_category(category_id: str, cache_dir: Optional[Path] = None) -> bool:
    cid = str(category_id or "").strip()
    if not is_valid_category_id(cid):
        return False
    try:
        from src.utils.category_tree_cache import is_cached_leaf_category

        return bool(is_cached_leaf_category(cid, cache_dir=cache_dir))
    except ImportError:
        # GrovePop does not ship the Main category-tree cache. A well-formed
        # numeric leaf is accepted here; publish callers still run their own
        # live leaf check. Empty / non-numeric ids already failed above.
        return True


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
    if not is_valid_category_id(cid):
        return ApprovedCategoryDecision(
            approved=True,
            preserve=False,
            error=APPROVED_CATEGORY_INVALID_ID_ERROR,
        )
    if _category_is_high_fee(cid, blacklist_path=blacklist_path):
        return ApprovedCategoryDecision(
            approved=True,
            preserve=False,
            error=APPROVED_CATEGORY_BLACKLIST_ERROR,
        )
    if not _is_sellable_leaf_category(cid, cache_dir=cache_dir):
        return ApprovedCategoryDecision(
            approved=True,
            preserve=False,
            error=approved_category_non_leaf_error(cid),
        )
    return ApprovedCategoryDecision(approved=True, preserve=True)
