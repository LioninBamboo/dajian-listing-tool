from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EBAY_LISTING_QUANTITY_CAP = 1
DEFAULT_EBAY_PUBLISH_FAILURE_FALLBACK_QUANTITY = DEFAULT_EBAY_LISTING_QUANTITY_CAP


class SupplierOutOfStockError(RuntimeError):
    """Raised when the supplier explicitly reports zero available inventory."""


@dataclass(frozen=True)
class QuantityAlignmentDecision:
    live_quantity: Optional[int]
    supplier_quantity: Optional[int]
    desired_quantity: Optional[int]
    status: str
    should_update: bool


def coerce_nonnegative_quantity(
    value: Any,
    *,
    fallback: Optional[int] = None,
) -> Optional[int]:
    try:
        quantity = int(float(value))
    except (TypeError, ValueError):
        return fallback
    return max(0, quantity)


def _listing_quantity_cap() -> Optional[int]:
    raw = str(os.getenv("EBAY_LISTING_QUANTITY_CAP", "") or "").strip()
    if not raw:
        return DEFAULT_EBAY_LISTING_QUANTITY_CAP
    try:
        cap = int(raw)
    except ValueError:
        return DEFAULT_EBAY_LISTING_QUANTITY_CAP
    return cap if cap > 0 else None


def _publish_failure_fallback_quantity() -> int:
    raw = str(os.getenv("EBAY_PUBLISH_FAILURE_FALLBACK_QUANTITY", "") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = DEFAULT_EBAY_PUBLISH_FAILURE_FALLBACK_QUANTITY
        if value > 0:
            return value

    cap = _listing_quantity_cap()
    if isinstance(cap, int) and cap > 0:
        return cap
    return DEFAULT_EBAY_PUBLISH_FAILURE_FALLBACK_QUANTITY


def normalize_ebay_listing_quantity(
    value: Any,
    *,
    fallback: int = 1,
    max_quantity: Optional[int] = None,
) -> int:
    base = max(1, int(fallback or 1))
    try:
        quantity = int(float(value))
    except (TypeError, ValueError):
        quantity = base

    quantity = max(base, quantity)
    cap = _listing_quantity_cap() if max_quantity is None else max_quantity
    if isinstance(cap, int) and cap > 0:
        quantity = min(quantity, cap)
    return quantity


def determine_target_ebay_quantity(
    supplier_quantity: Any,
    *,
    fallback: int = 1,
    max_quantity: Optional[int] = None,
) -> int:
    quantity = coerce_nonnegative_quantity(supplier_quantity)
    if quantity is None:
        raise ValueError("supplier quantity is not numeric")
    if quantity <= 0:
        return 0
    return normalize_ebay_listing_quantity(
        quantity,
        fallback=fallback,
        max_quantity=max_quantity,
    )


def assess_quantity_alignment(
    live_quantity: Any,
    supplier_quantity: Any,
    *,
    fallback: int = 1,
    max_quantity: Optional[int] = None,
) -> QuantityAlignmentDecision:
    live = coerce_nonnegative_quantity(live_quantity)
    supplier = coerce_nonnegative_quantity(supplier_quantity)

    if supplier is None:
        return QuantityAlignmentDecision(
            live_quantity=live,
            supplier_quantity=None,
            desired_quantity=None,
            status="supplier_unknown",
            should_update=False,
        )

    desired = determine_target_ebay_quantity(
        supplier,
        fallback=fallback,
        max_quantity=max_quantity,
    )

    if live is None:
        return QuantityAlignmentDecision(
            live_quantity=None,
            supplier_quantity=supplier,
            desired_quantity=desired,
            status="missing_live_quantity",
            should_update=False,
        )

    if desired == 0:
        if live == 0:
            return QuantityAlignmentDecision(
                live_quantity=live,
                supplier_quantity=supplier,
                desired_quantity=desired,
                status="aligned_out_of_stock",
                should_update=False,
            )
        return QuantityAlignmentDecision(
            live_quantity=live,
            supplier_quantity=supplier,
            desired_quantity=desired,
            status="supplier_out_of_stock",
            should_update=True,
        )

    if live == desired:
        return QuantityAlignmentDecision(
            live_quantity=live,
            supplier_quantity=supplier,
            desired_quantity=desired,
            status="aligned",
            should_update=False,
        )

    return QuantityAlignmentDecision(
        live_quantity=live,
        supplier_quantity=supplier,
        desired_quantity=desired,
        status="quantity_mismatch",
        should_update=True,
    )


def _build_dajian_client():
    load_dotenv(PROJECT_ROOT / ".env")

    from src.clients.dajian_client import DaJianClient

    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    if not client_id or not client_secret:
        raise ValueError("Missing DAJIAN_API_KEY or DAJIAN_API_SECRET in .env")
    return DaJianClient(client_id, client_secret)


def fetch_live_supplier_quantity(
    sku: str,
    *,
    dajian_client=None,
) -> Optional[int]:
    client = dajian_client or _build_dajian_client()
    stock_info = client.get_stock_info(sku)
    try:
        quantity = int(stock_info.get("quantity"))
    except (TypeError, ValueError, AttributeError):
        return None
    return max(0, quantity)


def resolve_publish_quantity(
    sku: str,
    *,
    fallback: int = 1,
    lookup_failure_fallback: Optional[int] = None,
    max_quantity: Optional[int] = None,
    dajian_client=None,
    logger: Optional[logging.Logger] = None,
) -> int:
    quantity = None
    try:
        quantity = fetch_live_supplier_quantity(sku, dajian_client=dajian_client)
    except Exception as exc:
        if logger is not None:
            logger.warning(f"  [{sku}] Failed to fetch live supplier quantity: {exc}")

    if quantity is None:
        fallback_quantity = (
            _publish_failure_fallback_quantity()
            if lookup_failure_fallback is None
            else lookup_failure_fallback
        )
        if logger is not None:
            logger.warning(
                f"  [{sku}] Falling back to publish quantity {fallback_quantity} because supplier quantity lookup returned no value"
            )
        return normalize_ebay_listing_quantity(
            fallback_quantity,
            fallback=fallback,
            max_quantity=max_quantity,
        )

    if quantity <= 0:
        raise SupplierOutOfStockError(f"{sku} has zero supplier inventory")

    return normalize_ebay_listing_quantity(
        quantity,
        fallback=fallback,
        max_quantity=max_quantity,
    )
