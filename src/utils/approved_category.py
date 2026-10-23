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
inherit *preservation* (``is_category_approved`` stays false). Automated
remaps still fail closed whenever the approval *flag* is set — including
blank / invalid / mismatched pins — unless a human passes
``--override-category-lock``.

Legacy rows that only have ``categoryApproved=true`` are still treated as
approved for the current ``categoryId``; persist the id with
``backfill_approved_category_id`` (``is_category_approved`` does
not mutate).
"""

from __future__ import annotations

import json
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
APPROVED_CATEGORY_INVALID_ID_ERROR = "invalid_or_blank_category_id"
PIN_MISMATCH_REPORT = "pin_mismatch"

_TRUTHY = {True, 1, "1", "true", "True", "yes", "Yes"}
logger = logging.getLogger(__name__)


def _as_optimization(optimization: Any) -> Mapping[str, Any] | None:
    """Accept a dict or a JSON object string (SQLite / publisher payload)."""
    if isinstance(optimization, Mapping):
        return optimization
    if isinstance(optimization, str) and optimization.strip():
        try:
            data = json.loads(optimization)
        except ValueError:
            return None
        return data if isinstance(data, Mapping) else None
    return None


def is_valid_category_id(category_id: str | None) -> bool:
    cid = str(category_id or "").strip()
    return bool(cid) and cid.isdigit()


def approved_category_id(optimization: Mapping[str, Any] | None) -> str:
    opt = _as_optimization(optimization)
    if opt is None:
        return ""
    return str(opt.get(CATEGORY_APPROVED_ID_KEY) or "").strip()


def category_approval_flag_set(optimization: Mapping[str, Any] | None) -> bool:
    """True when an operator stamped the approval flag, even if the pin is bad."""
    opt = _as_optimization(optimization)
    if opt is None:
        return False
    return opt.get(CATEGORY_APPROVED_KEY) in _TRUTHY


def is_category_approved(
    optimization: Mapping[str, Any] | None,
    category_id: str | None = None,
) -> bool:
    """Pin-equals-stored check. Callers that decide *whether automation may
    change the category* must use ``blocks_automated_category_change``.
    """
    opt = _as_optimization(optimization)
    if opt is None or not category_approval_flag_set(opt):
        return False

    current = str(category_id or opt.get("categoryId") or "").strip()
    pinned = approved_category_id(opt)
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


def pin_mismatch_reason(
    optimization: Mapping[str, Any] | None,
    category_id: str | None = None,
) -> str:
    """Report-only: flag set and pin ≠ stored id."""
    if not category_approval_flag_set(optimization):
        return ""
    opt = _as_optimization(optimization) or {}
    current = str(category_id or opt.get("categoryId") or "").strip()
    pinned = approved_category_id(opt)
    if pinned and current and pinned != current:
        return PIN_MISMATCH_REPORT
    return ""


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


def should_auto_remap_invalid_category(
    optimization: Mapping[str, Any] | None,
    *,
    preserve: bool = False,
    override: bool = False,
) -> bool:
    """Invalid-category publish retry may rematch only when unlocked."""
    if preserve:
        return False
    return not blocks_automated_category_change(optimization, override=override)


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


def plan_locked_override_revision(
    optimization: Mapping[str, Any] | None,
    live_category_id: str | None,
    *,
    explicit_target_id: str | None = None,
) -> dict[str, str]:
    """What ``--override-category-lock`` may do to a locked listing.

    Target is the operator's explicit per-SKU id, else ``categoryApprovedId``.
    Never a matcher rematch. Skip when live already equals that leaf, or when
    no approved / explicit target exists.
    """
    target = str(explicit_target_id or approved_category_id(optimization) or "").strip()
    live = str(live_category_id or "").strip()
    if not target:
        return {
            "action": "SKIP",
            "target_id": "",
            "reason": "locked with no categoryApprovedId and no explicit --target-category",
        }
    if live == target:
        return {
            "action": "SKIP",
            "target_id": target,
            "reason": f"live {live} already equals approved leaf {target}",
        }
    return {
        "action": "REVISE_LIVE",
        "target_id": target,
        "reason": f"restore approved leaf {target} (live={live or 'blank'})",
    }


def backfill_approved_category_id(
    optimization: dict[str, Any],
    category_id: str | None = None,
) -> str:
    """Persist ``categoryApprovedId`` for a legacy flag-only row."""
    if not isinstance(optimization, dict):
        return ""
    if not category_approval_flag_set(optimization):
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
    if not is_valid_category_id(cid):
        return ApprovedCategoryDecision(
            approved=True,
            preserve=False,
            error=APPROVED_CATEGORY_INVALID_ID_ERROR,
        )
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
