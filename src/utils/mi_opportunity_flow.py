from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.services.product_router import ARTTOY, AUTO, FURNITURE, classify_product
from src.utils.mi_draft_origin import MI_DRAFT_ORIGIN

MI_AUTO_PUBLISH_MIN_SCORE = 50
_READY_STATUSES = {"READY", "READY_TO_PUBLISH"}


def empty_auto_prepare_result() -> dict[str, Any]:
    return {
        "success": 0,
        "failed": 0,
        "requested": 0,
        "matched": 0,
        "prepared_skus": [],
    }


def auto_prepare_mi_opportunity_drafts(
    opportunities: list[dict[str, Any]] | None,
    *,
    analyze_collected_products: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    candidate_skus: list[str] = []
    seen_skus: set[str] = set()

    for opportunity in opportunities or []:
        if not isinstance(opportunity, dict):
            continue

        sku = str(opportunity.get("sku") or "").strip()
        status = str(opportunity.get("status") or "").strip().upper()
        if not sku or sku in seen_skus or status not in {"PENDING", "COLLECTED"}:
            continue

        seen_skus.add(sku)
        candidate_skus.append(sku)

    if not candidate_skus:
        return empty_auto_prepare_result()

    auto_prepare_result = analyze_collected_products(
        sku_filter=candidate_skus,
        eligible_statuses=("PENDING", "COLLECTED"),
        draft_origin=MI_DRAFT_ORIGIN,
    )
    prepared_skus = {
        str(sku).strip() for sku in (auto_prepare_result.get("prepared_skus") or []) if str(sku).strip()
    }

    for opportunity in opportunities or []:
        if not isinstance(opportunity, dict):
            continue
        sku = str(opportunity.get("sku") or "").strip()
        if sku in prepared_skus:
            opportunity["status"] = "READY"
            opportunity["draft_origin"] = MI_DRAFT_ORIGIN

    return auto_prepare_result


def _opportunity_score(opportunity: dict[str, Any]) -> float:
    raw = opportunity.get("opportunity_score")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _is_recommended(opportunity: dict[str, Any]) -> bool:
    recommendation = str(opportunity.get("recommendation") or "")
    if recommendation.startswith("❌"):
        return False
    return _opportunity_score(opportunity) >= MI_AUTO_PUBLISH_MIN_SCORE


def _store_kind_allows(title: str, store_kind: str) -> bool:
    target = classify_product(title=title).get("target")
    kind = str(store_kind or "furniture").strip().lower()
    if kind in {"furniture", "arttoy"} and target == AUTO:
        return False
    if kind == "auto" and target in {FURNITURE, ARTTOY}:
        return False
    return True


def lookup_supplier_stock(sku: str) -> int | None:
    """Best-effort GIGA qty. ``None`` means unknown; never raises."""
    try:
        from src.utils.ebay_quantity import fetch_live_supplier_quantity
        return fetch_live_supplier_quantity(sku)
    except Exception:
        return None


def select_mi_auto_publish_skus(
    opportunities: list[dict[str, Any]] | None,
    *,
    limit: int = 10,
    store_kind: str = "furniture",
    stock_lookup: Callable[[str], int | None] | None = None,
) -> list[str]:
    """READY MI SKUs that are allowed to enter auto-publish.

    Shared by scheduler_daemon and marketing_ops_digest. Stock lookup is
    optional: ``0`` skips the SKU, ``None`` (unknown / lookup failed) does not.
    """
    cap = max(1, int(limit or 10))
    selected: list[str] = []
    seen: set[str] = set()

    for opportunity in opportunities or []:
        if not isinstance(opportunity, dict):
            continue
        sku = str(opportunity.get("sku") or "").strip()
        status = str(opportunity.get("status") or "").strip().upper()
        if not sku or sku in seen or status not in _READY_STATUSES:
            continue
        if not _is_recommended(opportunity):
            continue
        title = str(opportunity.get("title") or "")
        if not _store_kind_allows(title, store_kind):
            continue
        if stock_lookup is not None:
            try:
                quantity = stock_lookup(sku)
            except Exception:
                quantity = None
            if quantity is not None and int(quantity) <= 0:
                continue
        seen.add(sku)
        selected.append(sku)
        if len(selected) >= cap:
            break
    return selected