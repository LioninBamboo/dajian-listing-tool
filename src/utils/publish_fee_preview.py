"""Insertion-fee preview helpers used immediately before a listing goes live."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional


HIGH_INSERTION_FEE_USD = 20.0
HIGH_FEE_PATH_MARKERS = (
    "vehicles/trailers",
    "cars & trucks",
    "cars and trucks",
)
HIGH_FEE_MOTORS_MARKERS = (
    "trailer",
    "trailers",
    "cars & trucks",
    "cars and trucks",
    "vehicles/trailers",
)


def load_high_fee_category_blacklist(path: Optional[str] = None) -> set[str]:
    raw_path = path if path is not None else os.getenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH")
    if not raw_path:
        return set()
    file_path = Path(raw_path)
    if not file_path.is_file():
        return set()
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, list):
        values = data
    elif isinstance(data, Mapping):
        values = data.get("categoryIds") or data.get("category_ids") or data.get("ids") or []
    else:
        return set()
    return {str(item).strip() for item in values if str(item).strip()}


def category_is_high_fee(
    category_id: str = "",
    category_path: str = "",
    category_name: str = "",
    blacklist_path: Optional[str] = None,
) -> bool:
    cat_id = str(category_id or "").strip()
    if cat_id and cat_id in load_high_fee_category_blacklist(blacklist_path):
        return True

    blob = " / ".join(
        part for part in (str(category_path or ""), str(category_name or "")) if part
    ).lower()
    if not blob:
        return False
    if any(marker in blob for marker in HIGH_FEE_PATH_MARKERS):
        return True
    if "ebay motors" in blob and any(marker in blob for marker in HIGH_FEE_MOTORS_MARKERS):
        return True
    return False


def insertion_fee_usd(listing_fees: Optional[Mapping[str, Any]]) -> Optional[float]:
    if not listing_fees:
        return None
    found: Optional[float] = None
    for summary in listing_fees.get("feeSummaries") or []:
        if not isinstance(summary, Mapping):
            continue
        for fee in summary.get("fees") or []:
            if not isinstance(fee, Mapping):
                continue
            if str(fee.get("feeType") or "").upper() != "INSERTION_FEE":
                continue
            amount = fee.get("amount") or {}
            raw = amount.get("value") if isinstance(amount, Mapping) else None
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            found = value if found is None else max(found, value)
    return found


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
