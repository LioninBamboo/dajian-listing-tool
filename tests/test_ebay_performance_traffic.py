"""Traffic report: top-200 truncation vs per-listing filter."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.services.ebay_performance import EbayPerformanceService


def _fake_response(records, status=200):
    r = MagicMock()
    r.status_code = status
    r.text = ""
    r.json.return_value = {
        "header": {
            "metrics": [
                {"key": "LISTING_IMPRESSION_TOTAL"},
                {"key": "LISTING_VIEWS_TOTAL"},
                {"key": "CLICK_THROUGH_RATE"},
                {"key": "SALES_CONVERSION_RATE"},
                {"key": "TRANSACTION"},
            ]
        },
        "records": records,
    }
    return r


def _rec(listing_id, imp=10, views=1):
    return {
        "dimensionValues": [{"value": str(listing_id)}],
        "metricValues": [
            {"value": imp, "applicable": True},
            {"value": views, "applicable": True},
            {"value": views / imp if imp else 0, "applicable": True},
            {"value": 0, "applicable": True},
            {"value": 0, "applicable": True},
        ],
    }


@patch("src.services.ebay_performance.requests.get")
@patch.object(EbayPerformanceService, "_headers", return_value={})
def test_fetch_traffic_top_n_marks_truncated(mock_headers, mock_get):
    svc = EbayPerformanceService.__new__(EbayPerformanceService)
    svc.base = "https://api.ebay.com"
    svc.oauth = MagicMock()
    # Exactly 200 records => truncated (API hard max)
    mock_get.return_value = _fake_response([_rec(i) for i in range(200)])
    out = svc.fetch_traffic_data(days=15)
    assert len(out) == 200
    assert svc.last_traffic_fetch_meta["api_ok"] is True
    assert svc.last_traffic_fetch_meta["is_truncated"] is True
    assert svc.last_traffic_fetch_meta["limit"] == 200
    assert svc.last_traffic_fetch_meta["record_count"] == 200
    params = mock_get.call_args.kwargs.get("params") or mock_get.call_args[1].get("params")
    assert "listing_ids" not in params["filter"]


@patch("src.services.ebay_performance.requests.get")
@patch.object(EbayPerformanceService, "_headers", return_value={})
def test_fetch_traffic_top_n_under_limit_not_truncated(mock_headers, mock_get):
    svc = EbayPerformanceService.__new__(EbayPerformanceService)
    svc.base = "https://api.ebay.com"
    svc.oauth = MagicMock()
    mock_get.return_value = _fake_response([_rec(i) for i in range(50)])
    out = svc.fetch_traffic_data(days=15)
    assert len(out) == 50
    assert svc.last_traffic_fetch_meta["is_truncated"] is False


@patch("src.services.ebay_performance.requests.get")
@patch.object(EbayPerformanceService, "_headers", return_value={})
def test_fetch_traffic_for_listing_uses_listing_ids_filter(mock_headers, mock_get):
    svc = EbayPerformanceService.__new__(EbayPerformanceService)
    svc.base = "https://api.ebay.com"
    svc.oauth = MagicMock()
    mock_get.return_value = _fake_response([_rec("L99", imp=7, views=2)])

    out = svc.fetch_traffic_for_listing("L99", days=15)

    assert out["impressions"] == 7
    assert out["views"] == 2
    params = mock_get.call_args.kwargs.get("params") or mock_get.call_args[1].get("params")
    assert "listing_ids:{L99}" in params["filter"]


@patch("src.services.ebay_performance.requests.get")
@patch.object(EbayPerformanceService, "_headers", return_value={})
def test_fetch_traffic_for_listing_empty_records_are_verified_zeros(mock_headers, mock_get):
    svc = EbayPerformanceService.__new__(EbayPerformanceService)
    svc.base = "https://api.ebay.com"
    svc.oauth = MagicMock()
    mock_get.return_value = _fake_response([])

    out = svc.fetch_traffic_for_listing("L0", days=90)

    assert out == {
        "impressions": 0,
        "views": 0,
        "ctr": 0.0,
        "conversion_rate": 0.0,
        "transactions": 0,
    }


@patch("src.services.ebay_performance.requests.get")
@patch.object(EbayPerformanceService, "_headers", return_value={})
def test_fetch_traffic_for_listing_api_error_returns_none(mock_headers, mock_get):
    svc = EbayPerformanceService.__new__(EbayPerformanceService)
    svc.base = "https://api.ebay.com"
    svc.oauth = MagicMock()
    mock_get.return_value = _fake_response([], status=500)

    assert svc.fetch_traffic_for_listing("L1", days=15) is None
