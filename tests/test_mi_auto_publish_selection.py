"""Shared MI auto-publish gates used by the daemon and store marketing digest."""
from src.utils.mi_opportunity_flow import select_mi_auto_publish_skus


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
