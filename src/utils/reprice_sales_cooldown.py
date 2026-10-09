"""Sales cooldown for the blanket (category-average) smart reprice.

Shared by every store instance. A SKU that sold recently has proven the
current price converts, so the twice-weekly catalog reprice must not move it.

CRO queue consume is conversion-driven and may still change those SKUs; that
exemption is the caller's choice (pass an empty sold set).
"""
from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(str(os.getenv(name, str(default))).strip()))
    except (TypeError, ValueError):
        return default


SALES_COOLDOWN_DAYS = _int_env("REPRICE_SALES_COOLDOWN_DAYS", 14)
SALES_COOLDOWN_MODE = (os.getenv("REPRICE_SALES_COOLDOWN_MODE", "hold") or "hold").strip().lower()


def cooldown_decision(in_cooldown: bool, price_diff: float,
                      mode: str = SALES_COOLDOWN_MODE) -> tuple[bool, str]:
    """Return ``(allow_change, hold_reason)``; ``hold_reason`` is '' when allowed.

    - mode 'hold':        block any change while in cooldown.
    - mode 'no_downside': block only price drops.
    """
    if not in_cooldown:
        return True, ""
    if mode == "no_downside":
        if price_diff < 0:
            return False, "recent_sale_no_downside"
        return True, ""
    return False, "recent_sale_hold"


def fetch_recently_sold_skus(cooldown_days: int, performance_service=None) -> tuple[set, dict]:
    """Best-effort SKUs with >=1 sale in the last ``cooldown_days``.

    On any error returns ``(set(), meta)`` with ``available=False`` so a
    research/API hiccup degrades to the legacy behaviour (reprice everything).
    """
    meta = {"available": False, "days": cooldown_days, "sku_count": 0, "error": None}
    if cooldown_days <= 0:
        meta["error"] = "disabled"
        return set(), meta
    try:
        if performance_service is None:
            from src.services.ebay_performance import EbayPerformanceService
            performance_service = EbayPerformanceService()
        sales = performance_service.fetch_sales_data(days=cooldown_days) or {}
        by_sku = sales.get("by_sku", {}) or {}
        skus = {
            str(sku)
            for sku, info in by_sku.items()
            if sku and float((info or {}).get("qty", 0) or 0) > 0
        }
        meta.update(available=True, sku_count=len(skus))
        return skus, meta
    except Exception as e:  # research must never hard-fail pricing
        meta["error"] = str(e)
        return set(), meta


def load_sales_cooldown(performance_service=None) -> tuple[set, dict]:
    """Load the sold-SKU set and attach the configured mode."""
    skus, meta = fetch_recently_sold_skus(
        SALES_COOLDOWN_DAYS, performance_service=performance_service
    )
    meta["mode"] = SALES_COOLDOWN_MODE
    return skus, meta
