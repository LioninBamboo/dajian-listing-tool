from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.utils.mi_draft_origin import MI_DRAFT_ORIGIN


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