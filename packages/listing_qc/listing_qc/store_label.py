"""Store-facing email labels. Callers inject brand_name; this module never reads store YAML."""

from __future__ import annotations

from typing import Callable, Optional


def store_email_label(
    brand_name: str | None = None,
    *,
    brand_getter: Optional[Callable[[], str]] = None,
    fallback: str = "eBay/GIGA",
) -> str:
    """Return a short brand label for audit/fix email subjects."""
    brand = str(brand_name or "").strip()
    if brand:
        return brand
    if brand_getter is not None:
        try:
            brand = str(brand_getter() or "").strip()
        except Exception:
            brand = ""
        if brand:
            return brand
    return fallback
