"""
Keyword fallbacks and retired-category remaps for EBAY_US (tree 0).

Main / Dajian only. Do not publish retired IDs 32880 or 42430.
139956 (Sheds) is an alternate for bin sheds and is never chosen here.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Tree 0 → tree 100. Main sells on tree 0, so this map stays empty.
# AquaRides (tree 100) is out of scope for this change. Self-check still
# walks the map when entries exist.
_MOTORS_TREE0_TO_TREE100: Dict[str, str] = {}

COAT_RACK_CATEGORY = ("22656", "Coat & Hat Racks")
DECK_BOX_CATEGORY = ("184533", "Storage Deck Boxes")
# Default for garbage bin shed / trash can storage.
# Alternate, comment only: 139956 Sheds.
TRASH_CAN_STORAGE_CATEGORY = ("161059", "Trash Can Storage")

# Most specific rule first. Bare "outdoor storage" is intentionally absent
# so that phrase goes to the Taxonomy suggestion path.
# 139956 Sheds is an alternate for bin sheds; this map always uses 161059.
_FALLBACK_RULES: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
    (("garbage bin shed", "trash can storage", "bin shed"), "161059", "Trash Can Storage"),
    (("deck box",), "184533", "Storage Deck Boxes"),
    (("coat rack", "coat stand"), "22656", "Coat & Hat Racks"),
)


class EbayCategoryMatcher:
    """Keyword category picker used when Taxonomy suggestions are unavailable."""

    def _fallback_category(self, text: str) -> Optional[Dict[str, str]]:
        """
        Return a leaf category dict, or None to force the API / human gate.

        Never returns retired 32880 / 42430, and never returns non-leaf 3197.
        """
        haystack = (text or "").lower()
        for keywords, category_id, category_name in _FALLBACK_RULES:
            if any(keyword in haystack for keyword in keywords):
                return {"categoryId": category_id, "categoryName": category_name}
        return None

    def hardcoded_categories(self) -> List[Tuple[str, str, str]]:
        """(category_id, name, tree_id) for the startup self-check."""
        rows = []
        seen = set()
        for _keywords, category_id, category_name in _FALLBACK_RULES:
            key = ("0", category_id)
            if key not in seen:
                seen.add(key)
                rows.append((category_id, category_name, "0"))
        for tree0_id, tree100_id in _MOTORS_TREE0_TO_TREE100.items():
            rows.append((str(tree100_id), f"motors:{tree0_id}", "100"))
        rows.extend(_listing_quality_gate_categories())
        return rows


def canonicalize_category(category_id: str, text: str = "") -> Optional[Dict[str, str]]:
    """
    Remap retired category IDs.

    32880 → 22656 Coat & Hat Racks.
    42430 → 161059 or 184533 from the title. None when the text does not say
    which outdoor product it is (do not keep 42430, do not guess).

    Any other ID is returned unchanged. Item specifics are not touched.
    """
    cid = str(category_id or "").strip()
    if not cid:
        return None
    if cid == "32880":
        return {
            "categoryId": COAT_RACK_CATEGORY[0],
            "categoryName": COAT_RACK_CATEGORY[1],
            "reason": "retired_32880",
        }
    if cid == "42430":
        mapped = EbayCategoryMatcher()._fallback_category(text)
        if mapped and mapped["categoryId"] in {DECK_BOX_CATEGORY[0], TRASH_CAN_STORAGE_CATEGORY[0]}:
            mapped = dict(mapped)
            mapped["reason"] = "retired_42430"
            return mapped
        return None
    return {"categoryId": cid, "categoryName": "", "reason": "unchanged"}


def is_category_plausible_for_text(category_id: str, category_name: str, text: str) -> bool:
    """
    Reject suggestions that match a loose keyword but the wrong leaf.

    Garbage-bin sheds must not land on Trash Cans & Wastebaskets (20608).
    139956 Sheds is a documented alternate and stays plausible, but the
    keyword map itself always prefers 161059.
    """
    text_l = (text or "").lower()
    name_l = (category_name or "").lower()
    cid = str(category_id or "")

    shed = any(k in text_l for k in ("garbage bin shed", "trash can storage", "bin shed"))
    if shed:
        if cid == "20608" or "wastebasket" in name_l:
            return False
        return cid in {"161059", "139956"} or "trash can storage" in name_l or "shed" in name_l

    if "deck box" in text_l:
        return cid == "184533" or "deck box" in name_l or "storage deck" in name_l

    if any(k in text_l for k in ("coat rack", "coat stand", "coat racks")):
        return cid == "22656" or "coat" in name_l or "hat rack" in name_l

    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", name_l)
        if len(token) > 3 and token not in {"home", "garden", "other", "accessories", "supplies"}
    ]
    if not tokens:
        return False
    return any(token in text_l for token in tokens)


def _is_api_category_plausible(category_id: str, category_name: str, text: str) -> bool:
    return is_category_plausible_for_text(category_id, category_name, text)


def _listing_quality_gate_categories() -> Iterable[Tuple[str, str, str]]:
    """Profile category IDs, when that module exists. Main has none in-tree."""
    try:
        from src.services import listing_quality_gate as gate
    except ImportError:
        return []
    rows = []
    profiles = getattr(gate, "CATEGORY_PROFILES", None) or getattr(gate, "PROFILES", None) or {}
    if isinstance(profiles, dict):
        values = profiles.values()
    else:
        values = profiles
    for profile in values:
        if not isinstance(profile, dict):
            continue
        cid = profile.get("categoryId") or profile.get("category_id")
        if cid:
            rows.append((str(cid), str(profile.get("categoryName") or profile.get("name") or ""), "0"))
    return rows


def run_startup_category_self_check(validator=None) -> List[dict]:
    """
    Validate every hardcoded fallback / motors / profile category.

    Logs a WARN for any ID that is not a current leaf. Network failures are
    warnings too: unknown is not treated as valid.
    """
    from src.services.ebay_category_validator import CategoryValidator

    checker = validator or CategoryValidator(write_cache=False)
    findings = []
    for category_id, name, tree_id in EbayCategoryMatcher().hardcoded_categories():
        result = checker.validate(category_id, tree_id)
        if not (result.get("valid") and result.get("leaf")):
            logger.warning(
                "[CAT_SELF_CHECK] WARN dead or unconfirmed category %s (%s) tree=%s reason=%s",
                category_id,
                name,
                tree_id,
                result.get("reason"),
            )
            findings.append({"categoryId": category_id, "treeId": tree_id, **result})
    return findings
