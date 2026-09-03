"""Shared MI auto-publish gates used by the daemon and store marketing digest."""
import json

from src.utils.mi_opportunity_flow import (
    load_today_factsheet_failed_skus,
    select_mi_auto_publish_skus,
    summarize_mi_auto_publish_gates,
)


def _opp(**overrides):
    row = {
        "sku": "SKU-1",
        "title": "Outdoor Patio Chair",
        "status": "READY",
        "opportunity_score": 62,
        "recommendation": "⚠️ 可以考虑 - 利润率29%, 需差异化运营",
    }
    row.update(overrides)
    return row


def test_skips_not_recommended_even_when_ready():
    skus = select_mi_auto_publish_skus(
        [
            _opp(sku="SKIP", opportunity_score=42, recommendation="❌ 暂不推荐 - 利润空间或市场竞争需优化"),
            _opp(sku="KEEP", opportunity_score=62),
        ],
        store_kind="furniture",
        limit=10,
    )
    assert skus == ["KEEP"]


def test_furniture_store_skips_auto_classified_titles():
    skus = select_mi_auto_publish_skus(
        [
            _opp(
                sku="AIR",
                title="Portable 5 Gallon Aluminum Air Tank with Pressure Gauge",
                opportunity_score=70,
            ),
            _opp(sku="CHAIR", title="Reclining Camping Chair Outdoor Patio", opportunity_score=70),
        ],
        store_kind="furniture",
        limit=10,
    )
    assert skus == ["CHAIR"]


def test_auto_store_skips_furniture_titles():
    skus = select_mi_auto_publish_skus(
        [
            _opp(sku="SOFA", title="Cloud Lazy Sofa Chair Microsuede", opportunity_score=80),
            _opp(sku="HITCH", title="Class 3 Tow Trailer Hitch 2 Inch Receiver", opportunity_score=80),
        ],
        store_kind="auto",
        limit=10,
    )
    assert skus == ["HITCH"]


def test_zero_supplier_stock_is_skipped_lookup_failure_stays():
    def stock(sku: str):
        if sku == "OOS":
            return 0
        if sku == "UNKNOWN":
            return None
        return 4

    skus = select_mi_auto_publish_skus(
        [
            _opp(sku="OOS", opportunity_score=80),
            _opp(sku="UNKNOWN", opportunity_score=80),
            _opp(sku="IN-STOCK", opportunity_score=80),
        ],
        store_kind="furniture",
        stock_lookup=stock,
        limit=10,
    )
    assert skus == ["UNKNOWN", "IN-STOCK"]


def test_exclude_skus_drops_today_factsheet_failures():
    skus = select_mi_auto_publish_skus(
        [
            _opp(sku="W206P305082", opportunity_score=62),
            _opp(sku="KEEP", opportunity_score=70),
        ],
        store_kind="furniture",
        limit=10,
        exclude_skus={"W206P305082"},
    )
    assert skus == ["KEEP"]


def test_load_today_factsheet_failed_skus(tmp_path):
    (tmp_path / "publish_results_20260903_113205.json").write_text(
        json.dumps([
            {
                "status": "error",
                "sku": "W206P305082",
                "message": "[FactSheet] semantic_feature (HIGH): adjustable speed",
            },
            {
                "status": "success",
                "sku": "OK-1",
                "message": "published",
            },
            {
                "status": "error",
                "sku": "OTHER",
                "message": "eBay timeout",
            },
        ]),
        encoding="utf-8",
    )
    (tmp_path / "publish_results_20260902_103234.json").write_text(
        json.dumps([
            {
                "status": "error",
                "sku": "YESTERDAY",
                "message": "[FactSheet] semantic_feature (HIGH): leftover",
            }
        ]),
        encoding="utf-8",
    )
    failed = load_today_factsheet_failed_skus(tmp_path, today="20260903")
    assert failed == {"W206P305082"}


def test_summarize_mi_auto_publish_gates_counts_ready_score_and_x():
    gates = summarize_mi_auto_publish_gates(
        [
            _opp(sku="LOW", opportunity_score=42, recommendation="❌ 暂不推荐"),
            _opp(sku="MID", opportunity_score=40, recommendation="⚠️ 可以考虑"),
            _opp(sku="KEEP", opportunity_score=62),
            {"sku": "PEND", "status": "PENDING", "opportunity_score": 90},
        ]
    )
    assert gates["ready"] == 3
    assert gates["below_min_score"] == 2
    assert gates["not_recommended"] == 1
    assert gates["min_score"] == 50
