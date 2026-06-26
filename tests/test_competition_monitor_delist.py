from __future__ import annotations

from src.web.pages.competition_monitor import (
    build_delist_suggestions,
    merge_performance_into_products,
)


def _product(
    sku: str,
    age_days: int,
    listing_id: str,
    *,
    has_sales: bool = False,
    health_score: int = 20,
):
    return {
        "sku": sku,
        "title": sku,
        "cat_name": "Beds",
        "listing_id": listing_id,
        "age_days": age_days,
        "health_score": health_score,
        "net_margin": 0.08,
        "has_sales": has_sales,
        "impressions": 0,
        "views": 0,
        "transactions": 0,
        "sold_qty": 1 if has_sales else 0,
    }


def test_merge_performance_marks_truncated_missing_traffic_as_uncovered():
    products = [_product("OLD", 90, "L-OLD")]
    perf_data = {
        "traffic": {},
        "traffic_meta": {
            "api_ok": True,
            "record_count": 200,
            "limit": 200,
            "is_truncated": True,
        },
        "sales": {"by_listing": {}, "by_sku": {}},
    }

    merged = merge_performance_into_products(products, perf_data)

    assert merged[0]["traffic_has_record"] is False
    assert merged[0]["traffic_data_status"] == "流量未覆盖"
    assert merged[0]["suspected_no_traffic"] is True
    assert merged[0]["confirmed_no_traffic"] is False


def test_build_delist_suggestions_requires_age_and_no_sales_and_sorts_by_age():
    products = [
        _product("NEW", 20, "L-NEW"),
        _product("SOLD", 100, "L-SOLD", has_sales=True),
        _product("OLDER", 100, "L-OLDER", health_score=30),
        _product("OLD", 80, "L-OLD", health_score=10),
    ]
    for p in products:
        p["traffic_data_status"] = "流量未覆盖"
        p["suspected_no_traffic"] = True
        p["confirmed_no_traffic"] = False

    suggestions = build_delist_suggestions(
        products,
        min_age_days=60,
        include_uncovered=True,
    )

    assert [p["sku"] for p in suggestions] == ["OLDER", "OLD"]


def test_build_delist_suggestions_can_require_confirmed_zero_traffic():
    products = [
        _product("UNCONFIRMED", 90, "L-U"),
        _product("CONFIRMED", 70, "L-C"),
    ]
    products[0]["traffic_data_status"] = "流量未覆盖"
    products[0]["suspected_no_traffic"] = True
    products[0]["confirmed_no_traffic"] = False
    products[1]["traffic_data_status"] = "确认零流量"
    products[1]["suspected_no_traffic"] = False
    products[1]["confirmed_no_traffic"] = True

    suggestions = build_delist_suggestions(
        products,
        min_age_days=60,
        include_uncovered=False,
    )

    assert [p["sku"] for p in suggestions] == ["CONFIRMED"]
