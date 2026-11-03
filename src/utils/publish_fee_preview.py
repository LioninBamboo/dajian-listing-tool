"""Insertion-fee preview helpers used immediately before a listing goes live."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional


HIGH_FEE_PATH_SEGMENTS = (
    "trailers",
    "other vehicles & trailers",
    "cars & trucks",
    "cars and trucks",
)
# Anchored Motors prefixes (not bare "Boats & Ships" under Collectibles / Toys).
HIGH_FEE_PATH_PREFIXES = (
    "ebay motors > powersports",
    "ebay motors > boats",
    "ebay motors > motorcycles",
    "ebay motors > other vehicles & trailers",
    "ebay motors > cars & trucks",
)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HIGH_FEE_BLACKLIST_RELATIVE = Path("data") / "config" / "high_fee_category_blacklist.json"
DEFAULT_HIGH_FEE_BLACKLIST_CANDIDATES = (
    DEFAULT_HIGH_FEE_BLACKLIST_RELATIVE,
    Path("data") / "config" / "publish_skip_categories.json",
    Path("data") / "config" / "publish_skip_category_ids.json",
    Path("..") / "Dajian_Listing_Tool" / "data" / "config" / "high_fee_category_blacklist.json",
)
_BLACKLIST_ID_KEYS = (
    "publish_skip_category_ids",
    "categoryIds",
    "category_ids",
    "ids",
)
_EMPTY_BLACKLIST_WARNED: set[str] = set()
logger = logging.getLogger(__name__)


def normalize_fee_type(value: str) -> str:
    return str(value or "").replace("_", "").upper()


def _current_store_aliases() -> set[str]:
    try:
        from src.utils.store_profile import get_store_profile

        brand = str(get_store_profile().brand_name or "")
    except Exception:
        brand = ""
    aliases = {
        "AquaVerve": {"main", "aquaverve", "aqua"},
        "GrovePop": {"grovepop", "gp"},
        "AquaRides": {"aqua", "aquarides", "autoparts"},
    }
    names = {brand.lower()} if brand else set()
    names.update(aliases.get(brand, set()))
    return {item for item in names if item}


def _blacklist_applies_to_store(data: Any) -> bool:
    if not isinstance(data, Mapping):
        return True
    applies = data.get("applies_to_stores")
    if not applies:
        return True
    aliases = _current_store_aliases()
    if not aliases:
        return True
    return any(str(item).strip().lower() in aliases for item in applies)


def resolve_high_fee_blacklist_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path)
    env_path = os.getenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH")
    if env_path:
        return Path(env_path)
    for relative in DEFAULT_HIGH_FEE_BLACKLIST_CANDIDATES:
        candidate = (REPO_ROOT / relative).resolve()
        if candidate.is_file():
            return candidate
    return REPO_ROOT / DEFAULT_HIGH_FEE_BLACKLIST_RELATIVE


def _read_blacklist_payload(path: Optional[str] = None) -> Any:
    file_path = resolve_high_fee_blacklist_path(path)
    source = str(file_path)
    if not file_path.is_file():
        _warn_empty_blacklist(source)
        return None
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _warn_empty_blacklist(source)
        return None


def _ids_from_blacklist_payload(data: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(data, list):
        return {str(item).strip() for item in data if str(item).strip()}
    if not isinstance(data, Mapping):
        return set()
    for key in _BLACKLIST_ID_KEYS:
        raw = data.get(key)
        if isinstance(raw, list):
            ids.update(str(item).strip() for item in raw if str(item).strip())
            break
    categories = data.get("categories")
    if isinstance(categories, list):
        for item in categories:
            if isinstance(item, Mapping):
                for key in ("id", "categoryId", "category_id"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        ids.add(value)
                        break
            else:
                value = str(item).strip()
                if value:
                    ids.add(value)
    return ids


def _paths_from_blacklist_payload(data: Any) -> dict[str, str]:
    if not isinstance(data, Mapping):
        return {}
    categories = data.get("categories")
    if not isinstance(categories, list):
        return {}
    paths: dict[str, str] = {}
    for item in categories:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        cid = ""
        for key in ("id", "categoryId", "category_id"):
            cid = str(item.get(key) or "").strip()
            if cid:
                break
        if cid:
            paths[cid] = path
    return paths


def _path_entries_from_blacklist_payload(data: Any) -> list[str]:
    if not isinstance(data, Mapping):
        return []
    categories = data.get("categories")
    if not isinstance(categories, list):
        return []
    return [
        str(item.get("path") or "").strip()
        for item in categories
        if isinstance(item, Mapping) and str(item.get("path") or "").strip()
    ]


def load_high_fee_category_blacklist(path: Optional[str] = None) -> set[str]:
    data = _read_blacklist_payload(path)
    if data is None:
        return set()
    if not _blacklist_applies_to_store(data):
        return set()
    ids = _ids_from_blacklist_payload(data)
    if not ids and not _path_entries_from_blacklist_payload(data):
        _warn_empty_blacklist(str(resolve_high_fee_blacklist_path(path)))
    return ids


def load_high_fee_blacklist_category_paths(path: Optional[str] = None) -> dict[str, str]:
    data = _read_blacklist_payload(path)
    if data is None:
        return {}
    return _paths_from_blacklist_payload(data)


def _warn_empty_blacklist(source: str) -> None:
    if source in _EMPTY_BLACKLIST_WARNED:
        return
    _EMPTY_BLACKLIST_WARNED.add(source)
    logger.warning("High-fee category blacklist loaded empty from %s", source)


def _path_segments(*parts: str) -> list[str]:
    text = " > ".join(str(part) for part in parts if str(part or "").strip())
    return [
        re.sub(r"\s+", " ", piece).strip().lower()
        for piece in re.split(r"\s*[/>]\s*", text)
        if piece.strip()
    ]


def category_is_high_fee(
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    blacklist_path: Optional[str] = None,
) -> bool:
    from src.utils.zero_fee_gate import REASON_HIGH_FEE_CATEGORY, category_match_reason

    return (
        category_match_reason(
            category_id,
            category_path,
            category_name,
            blacklist_path=blacklist_path,
        )
        == REASON_HIGH_FEE_CATEGORY
    )


def resolve_category_taxonomy_path(category_id: str, index=None) -> str:
    """Full taxonomy path from the on-disk tree index. Empty when unknown."""
    try:
        from src.utils.category_tree_cache import build_category_path

        return str(build_category_path(str(category_id or "").strip(), index=index) or "")
    except Exception:
        return ""


def blacklist_file_is_empty(blacklist_path: Optional[str] = None) -> bool:
    data = _read_blacklist_payload(blacklist_path)
    if data is None:
        return True
    if not _blacklist_applies_to_store(data):
        return True
    ids = _ids_from_blacklist_payload(data)
    paths = _path_entries_from_blacklist_payload(data)
    return not ids and not paths


def guard_category_write_high_fee(
    category_id: str = "",
    category_name: str = "",
    *,
    allow_unverified_fee: bool = False,
    index: Optional[Mapping[str, Any]] = None,
    blacklist_path: Optional[str] = None,
) -> Optional[str]:
    """Block a category write using the real GP/Aqua call shape.

    Resolves the full taxonomy path (not just the leaf name) before the
    path-segment check. ``get_listing_fees(offer_id)`` is intentionally not
    called here: it reflects the offer's *current* category, not the target.
    """
    from src.utils.zero_fee_gate import (
        REASON_BLACKLIST_UNAVAILABLE,
        REASON_CATEGORY_UNKNOWN,
        evaluate_fee_gate,
        human_block_reason,
    )

    cid = str(category_id or "").strip()
    path = resolve_category_taxonomy_path(cid, index=index)
    result = evaluate_fee_gate(
        category_id=cid,
        category_path=path,
        category_name=category_name,
        blacklist_path=blacklist_path,
    )
    if result.ok:
        # Path-segment / prefix rules cannot run without a taxonomy path.
        # Fail closed unless the caller explicitly allows an unverified write.
        if not path and not allow_unverified_fee:
            return (
                f"BLOCKED_HIGH_FEE_UNVERIFIED: category {cid or category_name or '?'} "
                "path unresolved"
            )
        return None
    if allow_unverified_fee and result.reason in {
        REASON_BLACKLIST_UNAVAILABLE,
        REASON_CATEGORY_UNKNOWN,
    }:
        return None
    return human_block_reason(result, category_name=category_name)


def high_fee_category_block_reason(
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    listing_fees: Optional[Mapping[str, Any]] = None,
    blacklist_path: Optional[str] = None,
) -> Optional[str]:
    """Human-readable BLOCKED_HIGH_FEE reason, or None if the write may proceed."""
    from src.utils.zero_fee_gate import evaluate_fee_gate, human_block_reason

    result = evaluate_fee_gate(
        category_id=category_id,
        category_path=category_path,
        category_name=category_name,
        listing_fees=listing_fees,
        lookup_attempted=listing_fees is not None,
        blacklist_path=blacklist_path,
    )
    return human_block_reason(result, category_name=category_name)


def evaluate_insertion_fee_preview(
    *,
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    listing_fees: Optional[Mapping[str, Any]] = None,
    lookup_error: Optional[BaseException] = None,
    lookup_attempted: bool = False,
    blacklist_path: Optional[str] = None,
) -> Optional[str]:
    from src.utils.zero_fee_gate import evaluate_fee_gate, wrapper_reason

    result = evaluate_fee_gate(
        category_id=category_id,
        category_path=category_path,
        category_name=category_name,
        listing_fees=listing_fees,
        lookup_error=lookup_error,
        lookup_attempted=lookup_attempted,
        blacklist_path=blacklist_path,
    )
    return wrapper_reason(result)
