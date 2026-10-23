"""Insertion-fee preview helpers used immediately before a listing goes live."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional


HIGH_INSERTION_FEE_USD = 20.0
HIGH_FEE_PATH_SEGMENTS = (
    "trailers",
    "other vehicles & trailers",
    "cars & trucks",
    "cars and trucks",
)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HIGH_FEE_BLACKLIST_RELATIVE = Path("data") / "config" / "high_fee_category_blacklist.json"
DEFAULT_HIGH_FEE_BLACKLIST_CANDIDATES = (
    DEFAULT_HIGH_FEE_BLACKLIST_RELATIVE,
    Path("data") / "config" / "publish_skip_categories.json",
    Path("data") / "config" / "publish_skip_category_ids.json",
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


def resolve_high_fee_blacklist_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path)
    env_path = os.getenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH")
    if env_path:
        return Path(env_path)
    for relative in DEFAULT_HIGH_FEE_BLACKLIST_CANDIDATES:
        candidate = REPO_ROOT / relative
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
    cat_id = str(category_id or "").strip()
    if cat_id and cat_id in load_high_fee_category_blacklist(blacklist_path):
        return True

    segments = _path_segments(category_path, category_name)
    if any(segment in HIGH_FEE_PATH_SEGMENTS for segment in segments):
        return True

    incoming = _path_segments(category_path)
    if incoming:
        for extra in _path_entries_from_blacklist_payload(_read_blacklist_payload(blacklist_path) or {}):
            if incoming == _path_segments(extra):
                return True
    return False


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
    cid = str(category_id or "").strip()
    path = resolve_category_taxonomy_path(cid, index=index)
    empty_blacklist = blacklist_file_is_empty(blacklist_path)
    if not path and empty_blacklist and not allow_unverified_fee:
        return (
            f"BLOCKED_HIGH_FEE_UNVERIFIED: category {cid or category_name or '?'} "
            "path unresolved and high-fee blacklist empty"
        )
    return high_fee_category_block_reason(
        category_id=cid,
        category_path=path,
        category_name=category_name,
        blacklist_path=blacklist_path,
    )


def high_fee_category_block_reason(
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    listing_fees: Optional[Mapping[str, Any]] = None,
    blacklist_path: Optional[str] = None,
) -> Optional[str]:
    """Human-readable BLOCKED_HIGH_FEE reason, or None if the write may proceed.

    Empty/missing blacklist still applies HIGH_FEE_PATH_SEGMENTS. A $20+
    insertion-fee preview fails closed. Missing preview is not a block here —
    callers that already have a fee payload must pass it.
    """
    cid = str(category_id or "").strip()
    # Touch the blacklist so a missing file logs its warning.
    load_high_fee_category_blacklist(blacklist_path)
    if category_is_high_fee(
        cid,
        category_path=category_path,
        category_name=category_name,
        blacklist_path=blacklist_path,
    ):
        return (
            f"BLOCKED_HIGH_FEE: category {cid or category_name or category_path} "
            "is on the high-fee blacklist or path-segment list"
        )
    if listing_fees is not None:
        fee = insertion_fee_usd(listing_fees)
        if fee is not None and fee >= HIGH_INSERTION_FEE_USD:
            return (
                f"BLOCKED_HIGH_FEE: category {cid} insertion fee ${fee:.2f} >= "
                f"${HIGH_INSERTION_FEE_USD:.2f}"
            )
    return None


def insertion_fee_usd(listing_fees: Optional[Mapping[str, Any]]) -> Optional[float]:
    if listing_fees is None or not isinstance(listing_fees, Mapping):
        return None
    found: Optional[float] = None
    target = normalize_fee_type("INSERTION_FEE")
    for summary in listing_fees.get("feeSummaries") or []:
        if not isinstance(summary, Mapping):
            continue
        for fee in summary.get("fees") or []:
            if not isinstance(fee, Mapping):
                continue
            if normalize_fee_type(str(fee.get("feeType") or "")) != target:
                continue
            amount = fee.get("amount") or {}
            raw = amount.get("value") if isinstance(amount, Mapping) else None
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            found = value if found is None else max(found, value)
    # A successful fee payload with no insertion-fee line is $0, not unavailable.
    return 0.0 if found is None else found


def allow_publish_without_fee_preview() -> bool:
    return str(os.getenv("ALLOW_PUBLISH_WITHOUT_FEE_PREVIEW") or "").strip() == "1"


def evaluate_insertion_fee_preview(
    *,
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    listing_fees: Optional[Mapping[str, Any]] = None,
    lookup_error: Optional[BaseException] = None,
    lookup_attempted: bool = False,
    blacklist_path: Optional[str] = None,
    allow_without_preview: Optional[bool] = None,
) -> Optional[str]:
    if category_is_high_fee(
        category_id,
        category_path=category_path,
        category_name=category_name,
        blacklist_path=blacklist_path,
    ):
        return "high_insertion_fee"

    if lookup_error is not None or (lookup_attempted and listing_fees is None):
        if allow_without_preview is None:
            allow_without_preview = allow_publish_without_fee_preview()
        return None if allow_without_preview else "fee_preview_unavailable"

    if listing_fees is None:
        return None

    fee = insertion_fee_usd(listing_fees)
    if fee is None:
        if allow_without_preview is None:
            allow_without_preview = allow_publish_without_fee_preview()
        return None if allow_without_preview else "fee_preview_unavailable"
    if fee >= HIGH_INSERTION_FEE_USD:
        return "high_insertion_fee"
    return None
