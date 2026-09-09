"""Phase 0: GIGA fulfillment methods on DaJianClient."""
from __future__ import annotations

import pytest

from src.clients.dajian_client import DaJianClient


@pytest.fixture
def client():
    return DaJianClient("test-id", "test-secret")


def test_chunk_list():
    assert DaJianClient._chunk_list([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_query_warehouse_addresses_batches_and_posts(client, monkeypatch):
    calls = []

    def fake_request(method, endpoint, json_data=None, **kwargs):
        calls.append((method, endpoint, json_data))
        codes = json_data["warehouseCodes"]
        return [{"warehouseCode": c, "city": "X"} for c in codes]

    monkeypatch.setattr(client, "_request", fake_request)
    codes = [f"W{i}" for i in range(250)]
    out = client.query_warehouse_addresses(codes)
    assert len(out) == 250
    assert len(calls) == 2  # 200 + 50
    assert calls[0][1] == "/buyer/warehouse/query-address/v1"
    assert len(calls[0][2]["warehouseCodes"]) == 200
    assert len(calls[1][2]["warehouseCodes"]) == 50


def test_query_order_tracking_and_status(client, monkeypatch):
    seen = []

    def fake_request(method, endpoint, json_data=None, **kwargs):
        seen.append(endpoint)
        return [{"orderNo": n} for n in json_data["orderNo"]]

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.query_order_tracking(["A", "B"])[0]["orderNo"] == "A"
    assert client.query_order_status(["C"])[0]["orderNo"] == "C"
    assert seen == ["/buyer/order/track-no/v1", "/buyer/order/status/v1"]


def test_validate_dropship_payload_ok_without_warehouse():
    payload = {
        "orderDate": "2025-07-30 12:00:00",
        "orderNo": "EB12345",
        "shipName": "Tom",
        "shipPhone": "1234567890",
        "shipAddress1": "1 Main St",
        "shipCity": "LA",
        "shipCountry": "US",
        "shipZipCode": "90001",
        # non-eBay channel: item/txn ids optional
        "salesChannel": "Other",
        "orderLines": [{"sku": "W1", "qty": 1, "itemPrice": 10}],
    }
    # One-piece dropship: no warehouseCode required — GIGA chooses the warehouse.
    assert DaJianClient.validate_dropship_payload(payload) == []

def test_validate_dropship_payload_catches_missing_fields():
    errs = DaJianClient.validate_dropship_payload({"orderNo": "bad space"})
    assert any("orderDate" in e for e in errs)
    assert any("orderLines" in e for e in errs)
    assert any("orderNo may only" in e for e in errs)


def test_validate_dropship_payload_rejects_po_box():
    payload = {
        "orderDate": "2025-07-30 12:00:00",
        "orderNo": "EB12345",
        "shipName": "Tom",
        "shipPhone": "1234567890",
        "shipAddress1": "P.O. Box 123",
        "shipCity": "LA",
        "shipCountry": "US",
        "shipZipCode": "90001",
        "salesChannel": "Other",
        "orderLines": [{"sku": "W1", "qty": 1, "itemPrice": 10}],
    }

    errors = DaJianClient.validate_dropship_payload(payload)

    assert any("PO Box" in error for error in errors)


def test_import_dropship_dry_run_does_not_call_api(client, monkeypatch):
    monkeypatch.setattr(
        client,
        "_request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call API")),
    )
    payload = {
        "orderDate": "2025-07-30 12:00:00",
        "orderNo": "EB12345",
        "shipName": "Tom",
        "shipPhone": "1234567890",
        "shipAddress1": "1 Main St",
        "shipCity": "LA",
        "shipCountry": "US",
        "shipZipCode": "90001",
        "orderLines": [
            {"sku": "W1", "qty": 1, "itemPrice": 10, "ebayItemCode": "366518074803"}
        ],
        "salesChannel": "eBay",
        "ebayTransactionID": "10085330527613",
    }
    out = client.import_dropship_order(payload, dry_run=True)
    assert out["dry_run"] is True
    assert "dropShip-sync" in out["endpoint"]
    assert "warehouse" in out["note"].lower()
    assert out["payload"]["orderNo"] == "EB12345"


def test_import_dropship_live_posts(client, monkeypatch):
    calls = []

    def fake_request(method, endpoint, json_data=None, **kwargs):
        calls.append((endpoint, json_data))
        return None  # GIGA often returns null data on success

    monkeypatch.setattr(client, "_request", fake_request)
    payload = {
        "orderDate": "2025-07-30 12:00:00",
        "orderNo": "EB99",
        "shipName": "Tom",
        "shipPhone": "1234567890",
        "shipAddress1": "1 Main St",
        "shipCity": "LA",
        "shipCountry": "US",
        "shipZipCode": "90001",
        "salesChannel": "eBay",
        "ebayTransactionID": "10085330527613",
        "orderLines": [
            {"sku": "W1", "qty": 1, "itemPrice": 10, "ebayItemCode": "366518074803"}
        ],
    }
    out = client.import_dropship_order(payload, dry_run=False)
    assert out["dry_run"] is False
    assert calls[0][0] == "/buyer/order/dropShip-sync/v1"
    assert "warehouseCode" not in calls[0][1]


def test_import_dropship_live_disables_transport_retries(client, monkeypatch):
    seen = {}

    def fake_request(method, endpoint, json_data=None, **kwargs):
        seen.update(method=method, endpoint=endpoint, kwargs=kwargs)
        return None

    monkeypatch.setattr(client, "_request", fake_request)
    payload = {
        "orderDate": "2025-07-30 12:00:00",
        "orderNo": "EB99",
        "shipName": "Tom",
        "shipPhone": "1234567890",
        "shipAddress1": "1 Main St",
        "shipCity": "LA",
        "shipCountry": "US",
        "shipZipCode": "90001",
        "salesChannel": "eBay",
        "ebayTransactionID": "10085330527613",
        "orderLines": [
            {"sku": "W1", "qty": 1, "itemPrice": 10, "ebayItemCode": "366518074803"}
        ],
    }

    client.import_dropship_order(payload, dry_run=False)

    assert seen["kwargs"]["retries"] == 0
    assert seen["kwargs"]["retry_transport"] is False


def test_non_idempotent_request_does_not_retry_after_proxy_timeout(client, monkeypatch):
    import requests

    calls = []

    class _NoRetrySession:
        def request(self, **kwargs):
            calls.append(kwargs.get("proxies"))
            raise requests.exceptions.Timeout("ambiguous write timeout")

    monkeypatch.setenv("DAJIAN_PROXY_URL", "http://127.0.0.1:10808")
    monkeypatch.setattr(
        "src.clients.dajian_client._is_local_proxy_available",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "src.clients.dajian_client._create_dajian_session",
        lambda **_kwargs: _NoRetrySession(),
    )

    with pytest.raises(Exception, match="API Timeout"):
        client._request(
            "POST",
            "/buyer/order/dropShip-sync/v1",
            json_data={"orderNo": "OID1"},
            retries=0,
            retry_transport=False,
        )

    assert len(calls) == 1


def test_import_pickup_label_requires_label(client):
    with pytest.raises(ValueError, match="labelFile"):
        client.import_pickup_label_order(
            {
                "orderDate": "2025-07-30",
                "orderNo": "P1",
                "shipMethod": "FedEx",
                "salesChannel": "eBay",
                "orderLines": [{"sku": "W1", "qty": 1}],
            },
            dry_run=True,
        )


def test_import_pickup_label_dry_run_redacts_base64(client, monkeypatch):
    monkeypatch.setattr(
        client,
        "_request",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no api")),
    )
    out = client.import_pickup_label_order(
        {
            "orderDate": "2025-07-30",
            "orderNo": "P1",
            "shipMethod": "FedEx",
            "salesChannel": "eBay",
            "orderLines": [{"sku": "W1", "qty": 1}],
            "labelFile": ["AAAA"],
        },
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert "base64" in out["payload_preview"]["labelFile"][0]
