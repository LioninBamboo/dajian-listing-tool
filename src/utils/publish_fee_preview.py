"""Insertion-fee preview helpers used immediately before a listing goes live."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Optional


HIGH_INSERTION_FEE_USD = 20.0
HIGH_FEE_PATH_MARKERS = (
    "vehicles/trailers",
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


def _ids_from_blacklist_payload(data: Any) -> set[str]:
    if isinstance(data, list):
        values = data
    elif isinstance(data, Mapping):
        values = []
        for key in _BLACKLIST_ID_KEYS:
            raw = data.get(key)
            if isinstance(raw, list):
                values = raw
                break
    else:
        return set()
    return {str(item).strip() for item in values if str(item).strip()}


def load_high_fee_category_blacklist(path: Optional[str] = None) -> set[str]:
    file_path = resolve_high_fee_blacklist_path(path)
    source = str(file_path)
    if not file_path.is_file():
        _warn_empty_blacklist(source)
        return set()
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _warn_empty_blacklist(source)
        return set()
    ids = _ids_from_blacklist_payload(data)
    if not ids:
        _warn_empty_blacklist(source)
    return ids


def _warn_empty_blacklist(source: str) -> None:
    if source in _EMPTY_BLACKLIST_WARNED:
        return
    _EMPTY_BLACKLIST_WARNED.add(source)
    logger.warning("High-fee category blacklist loaded empty from %s", source)


def _path_blobs(category_path: str = "", category_name: str = "") -> tuple[str, str]:
    blob = " / ".join(
        part for part in (str(category_path or ""), str(category_name or "")) if part
    ).lower()
    compact = re.sub(r"\s*/\s*", "/", blob)
    return blob, compact


def category_is_high_fee(
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    blacklist_path: Optional[str] = None,
) -> bool:
    cat_id = str(category_id or "").strip()
    if cat_id and cat_id in load_high_fee_category_blacklist(blacklist_path):
        return True

    blob, compact = _path_blobs(category_path, category_name)
    if not blob:
        return False
    return any(
        marker in blob or marker in compact
        for marker in HIGH_FEE_PATH_MARKERS
    )


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
