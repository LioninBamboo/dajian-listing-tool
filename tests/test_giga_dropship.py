"""Phase 1: eBay order → GIGA dropship payload mapping."""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from src.services.giga_dropship import (
    build_dropship_payload_from_ebay_order,
    ensure_fulfillment_table,
    find_inventory_shortages,
    get_fulfillment_row,
    is_ambiguous_dropship_push_error,
    is_dropship_candidate,
    make_giga_order_no,
    plan_dropship_batch,
    reserve_fulfillment_push,
    record_fulfillment_attempt,
)


SAMPLE_ORDER = {
    "orderId": "04-15027-11320",
    "legacyOrderId": "04-15027-11320",
    "creationDate": "2026-08-11T04:51:02.000Z",
    "orderFulfillmentStatus": "NOT_STARTED",
    "orderPaymentStatus": "PAID",
    "pricingSummary": {"total": {"value": "227.16", "currency": "USD"}},
    "fulfillmentStartInstructions": [
        {
            "shippingStep": {
                "shipTo": {
                    "fullName": "Gerard Gusman",
                    "email": "buyer@example.com",
                    "primaryPhone": {"phoneNumber": "5043193261"},
                    "contactAddress": {
                        "addressLine1": "1605 Crossmoor Dr",
                        "city": "Marrero",
                        "stateOrProvince": "LA",
                        "postalCode": "70072-4057",
                        "countryCode": "US",
                    },
                }
            }
        }
    ],
    "lineItems": [
        {
            "sku": "W3118P505149",
            "title": "66 Inch Bench",
            "quantity": 1,
            "lineItemId": "10083614296404",  # eBay Transaction ID
            "legacyItemId": "366547297749",  # eBay Item Number
            "lineItemCost": {"value": "227.16", "currency": "USD"},
        }
    ],
}


def test_make_giga_order_no_uses_ebay_order_id_as_is():
    # Template *OrderId = eBay Order Number — no forced EB prefix
    assert make_giga_order_no("13-15010-93245") == "13-15010-93245"
    assert make_giga_order_no("04-15027-11320") == "04-15027-11320"
    # Illegal chars still sanitized
    no = make_giga_order_no("bad order/id")
    assert " " not in no
    assert "/" not in no
    assert all(c.isalnum() or c in "._-" for c in no)


def test_is_dropship_candidate_requires_paid_unshipped():
    assert is_dropship_candidate(SAMPLE_ORDER) is True
    o2 = dict(SAMPLE_ORDER, orderPaymentStatus="PENDING")
    assert is_dropship_candidate(o2) is False
    o2_refunded = dict(SAMPLE_ORDER, orderPaymentStatus="PARTIALLY_REFUNDED")
    assert is_dropship_candidate(o2_refunded) is False
    o3 = dict(SAMPLE_ORDER, orderFulfillmentStatus="FULFILLED")
    assert is_dropship_candidate(o3) is False


def test_build_payload_maps_ship_to_and_sku_without_warehouse():
    payload = build_dropship_payload_from_ebay_order(SAMPLE_ORDER)
    assert payload["orderNo"] == "04-15027-11320"
    assert payload["shipName"] == "Gerard Gusman"

    assert payload["shipCity"] == "Marrero"
    assert payload["shipCountry"] == "US"
    assert payload["salesChannel"] == "eBay"
    assert payload["orderLines"][0]["sku"] == "W3118P505149"
    assert payload["orderLines"][0]["qty"] == 1
    # Template *eBayItemNumber = Item Number = legacyItemId
    assert payload["orderLines"][0]["ebayItemCode"] == "366547297749"
    # Template *eBayTransactionID = Transaction ID = lineItemId (NOT order number digits)
    assert payload["ebayTransactionID"] == "10083614296404"
    assert payload["ebayTransactionID"] != "041502711320"
    assert "warehouseCode" not in payload
    assert payload["valueAddedServices"]["deliveryService"] == "DSR"

    from src.clients.dajian_client import DaJianClient

    assert DaJianClient.validate_dropship_payload(payload) == []


def test_ebay_channel_requires_item_and_transaction_ids():
    from src.clients.dajian_client import DaJianClient

    payload = build_dropship_payload_from_ebay_order(SAMPLE_ORDER)
    payload.pop("ebayTransactionID", None)
    payload["orderLines"][0].pop("ebayItemCode", None)
    errs = DaJianClient.validate_dropship_payload(payload)
    assert any("ebayTransactionID" in e for e in errs)
    assert any("ebayItemCode" in e for e in errs)

def test_plan_dropship_batch_skips_pushed(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    ensure_fulfillment_table(conn)
    record_fulfillment_attempt(
        conn,
        ebay_order_id="04-15027-11320",
        giga_order_no="04-15027-11320",
        status="pushed",
        payload={},
    )
    plans = plan_dropship_batch(
        [SAMPLE_ORDER],
        skip_already_pushed=True,
        conn=conn,
    )
    assert plans == []

    plans2 = plan_dropship_batch(
        [SAMPLE_ORDER],
        skip_already_pushed=False,
        conn=conn,
    )
    assert len(plans2) == 1
    assert plans2[0]["ok"] is True
    conn.close()


def test_plan_dropship_batch_skips_still_processing(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    ensure_fulfillment_table(conn)
    record_fulfillment_attempt(
        conn,
        ebay_order_id="04-15027-11320",
        giga_order_no="04-15027-11320",
        status="still_processing",
        payload={},
    )
    plans = plan_dropship_batch(
        [SAMPLE_ORDER],
        skip_already_pushed=True,
        conn=conn,
    )
    assert plans == []
    conn.close()


@pytest.mark.parametrize(
    "prior_status",
    ["push_in_progress", "push_unknown", "fulfill_failed", "sync_error"],
)
def test_plan_dropship_batch_skips_orders_already_accepted_by_giga(tmp_path, prior_status):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    ensure_fulfillment_table(conn)
    record_fulfillment_attempt(
        conn,
        ebay_order_id="04-15027-11320",
        giga_order_no="04-15027-11320",
        status=prior_status,
        payload={"orderNo": "04-15027-11320"},
    )

    plans = plan_dropship_batch(
        [SAMPLE_ORDER],
        skip_already_pushed=True,
        conn=conn,
    )

    assert plans == []
    conn.close()


@pytest.mark.parametrize(
    "prior_status",
    ["dry_run_ready", "validation_failed", "stock_blocked", "stock_check_failed", "push_failed"],
)
def test_plan_dropship_batch_retries_preflight_statuses(tmp_path, prior_status):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    ensure_fulfillment_table(conn)
    record_fulfillment_attempt(
        conn,
        ebay_order_id="04-15027-11320",
        giga_order_no="04-15027-11320",
        status=prior_status,
        payload={"orderNo": "04-15027-11320"},
    )

    plans = plan_dropship_batch(
        [SAMPLE_ORDER],
        skip_already_pushed=True,
        conn=conn,
    )

    assert len(plans) == 1
    assert plans[0]["prior_status"] == prior_status
    conn.close()


def test_find_inventory_shortages_fails_closed_for_missing_or_insufficient_stock():
    payload = build_dropship_payload_from_ebay_order(SAMPLE_ORDER)

    assert find_inventory_shortages(payload, {}) == [
        "W3118P505149: inventory response missing"
    ]
    shortages = find_inventory_shortages(
        payload,
        {
            "W3118P505149": {
                "sku": "W3118P505149",
                "buyerInventoryInfo": {"totalBuyerAvailableInventory": 0},
                "sellerInventoryInfo": {"sellerAvailableInventory": 0},
            }
        },
    )
    assert shortages == ["W3118P505149: requested 1, available 0"]


def test_live_push_records_inventory_read_failure_without_reserving_or_writing(
    tmp_path, monkeypatch
):
    import scripts.giga_dropship_push as push

    class FakeDaJianClient:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def validate_dropship_payload(_payload):
            return []

        def get_inventory(self, _skus):
            raise RuntimeError("inventory API unavailable")

        def import_dropship_order(self, *_args, **_kwargs):
            raise AssertionError("inventory failure must happen before GIGA write")

    monkeypatch.setattr(
        "src.services.giga_dropship.fetch_ebay_orders",
        lambda **_kwargs: [SAMPLE_ORDER],
    )
    monkeypatch.setattr("src.clients.dajian_client.DaJianClient", FakeDaJianClient)
    monkeypatch.setenv("DAJIAN_API_KEY", "test-key")
    monkeypatch.setenv("DAJIAN_API_SECRET", "test-secret")
    db_path = tmp_path / "eBay.db"
    out_path = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "giga_dropship_push.py",
            "--apply",
            "--db",
            str(db_path),
            "--out",
            str(out_path),
        ],
    )

    assert push.main() == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["results"][0]["status"] == "stock_check_failed"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT status FROM giga_fulfillment_orders WHERE ebay_order_id = ?",
        (SAMPLE_ORDER["orderId"],),
    ).fetchone()
    assert row == ("stock_check_failed",)
    conn.close()


def test_record_status_update_preserves_original_payload(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    payload = {"orderNo": "OID1", "shipName": "Test"}
    record_fulfillment_attempt(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        status="pushed",
        payload=payload,
    )
    record_fulfillment_attempt(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        status="sync_error",
        error="temporary",
    )

    row = get_fulfillment_row(conn, "OID1")
    assert json.loads(row["payload_json"]) == payload
    conn.close()


def test_reserve_fulfillment_push_is_atomic(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    payload = {"orderNo": "OID1"}

    assert reserve_fulfillment_push(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        payload=payload,
    ) is True
    assert reserve_fulfillment_push(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        payload=payload,
    ) is False
    assert get_fulfillment_row(conn, "OID1")["status"] == "push_in_progress"
    conn.close()


def test_reserve_fulfillment_push_can_promote_dry_run_row_once(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    payload = {"orderNo": "OID1"}
    record_fulfillment_attempt(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        status="dry_run_ready",
        payload=payload,
    )

    assert reserve_fulfillment_push(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        payload=payload,
    ) is True
    assert reserve_fulfillment_push(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        payload=payload,
    ) is False
    assert get_fulfillment_row(conn, "OID1")["status"] == "push_in_progress"
    conn.close()


def test_record_and_get_fulfillment_row(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    record_fulfillment_attempt(
        conn,
        ebay_order_id="OID1",
        giga_order_no="EBOID1",
        status="dry_run_ready",
        payload={"orderNo": "EBOID1"},
    )
    row = get_fulfillment_row(conn, "OID1")
    assert row["status"] == "dry_run_ready"
    assert json.loads(row["payload_json"])["orderNo"] == "EBOID1"
    conn.close()


def test_is_ambiguous_dropship_push_error_classifies_timeouts_vs_rejections():
    import requests

    assert is_ambiguous_dropship_push_error(Exception("API Timeout: https://giga.example/dropship"))
    assert is_ambiguous_dropship_push_error(
        Exception("Connection Error (after urllib3 retries): reset")
    )
    assert not is_ambiguous_dropship_push_error(
        Exception("API Error: PO Box addresses are not supported")
    )
    assert not is_ambiguous_dropship_push_error(ValueError("dropship payload invalid: shipZipCode"))

    http_400 = requests.exceptions.HTTPError("400")
    http_400.response = type("Resp", (), {"status_code": 400})()
    assert not is_ambiguous_dropship_push_error(http_400)

    http_503 = requests.exceptions.HTTPError("503")
    http_503.response = type("Resp", (), {"status_code": 503})()
    assert is_ambiguous_dropship_push_error(http_503)


def test_reserve_fulfillment_push_can_promote_push_failed_row_once(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    payload = {"orderNo": "OID1"}
    record_fulfillment_attempt(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        status="push_failed",
        payload=payload,
        error="API Error: bad address",
    )

    assert reserve_fulfillment_push(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        payload=payload,
    ) is True
    assert reserve_fulfillment_push(
        conn,
        ebay_order_id="OID1",
        giga_order_no="OID1",
        payload=payload,
    ) is False
    assert get_fulfillment_row(conn, "OID1")["status"] == "push_in_progress"
    conn.close()


def _stocked_inventory_item(sku: str) -> dict:
    return {
        "sku": sku,
        "buyerInventoryInfo": {"totalBuyerAvailableInventory": 10},
        "sellerInventoryInfo": {"sellerAvailableInventory": 10},
    }


def test_live_push_records_clear_giga_rejection_as_push_failed(tmp_path, monkeypatch):
    import scripts.giga_dropship_push as push

    class FakeDaJianClient:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def validate_dropship_payload(_payload):
            return []

        def get_inventory(self, skus):
            return [_stocked_inventory_item(sku) for sku in skus]

        def import_dropship_order(self, *_args, **_kwargs):
            raise Exception("API Error: PO Box addresses are not supported")

    monkeypatch.setattr(
        "src.services.giga_dropship.fetch_ebay_orders",
        lambda **_kwargs: [SAMPLE_ORDER],
    )
    monkeypatch.setattr("src.clients.dajian_client.DaJianClient", FakeDaJianClient)
    monkeypatch.setenv("DAJIAN_API_KEY", "test-key")
    monkeypatch.setenv("DAJIAN_API_SECRET", "test-secret")
    db_path = tmp_path / "eBay.db"
    out_path = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "giga_dropship_push.py",
            "--apply",
            "--db",
            str(db_path),
            "--out",
            str(out_path),
        ],
    )

    assert push.main() == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["results"][0]["status"] == "push_failed"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT status FROM giga_fulfillment_orders WHERE ebay_order_id = ?",
        (SAMPLE_ORDER["orderId"],),
    ).fetchone()
    assert row == ("push_failed",)
    conn.close()

    conn = sqlite3.connect(str(db_path))
    plans = plan_dropship_batch(
        [SAMPLE_ORDER],
        skip_already_pushed=True,
        conn=conn,
    )
    assert len(plans) == 1
    conn.close()


def test_live_push_records_timeout_as_push_unknown(tmp_path, monkeypatch):
    import scripts.giga_dropship_push as push

    class FakeDaJianClient:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def validate_dropship_payload(_payload):
            return []

        def get_inventory(self, skus):
            return [_stocked_inventory_item(sku) for sku in skus]

        def import_dropship_order(self, *_args, **_kwargs):
            raise Exception("API Timeout: https://giga.example/dropShip-sync/v1")

    monkeypatch.setattr(
        "src.services.giga_dropship.fetch_ebay_orders",
        lambda **_kwargs: [SAMPLE_ORDER],
    )
    monkeypatch.setattr("src.clients.dajian_client.DaJianClient", FakeDaJianClient)
    monkeypatch.setenv("DAJIAN_API_KEY", "test-key")
    monkeypatch.setenv("DAJIAN_API_SECRET", "test-secret")
    db_path = tmp_path / "eBay.db"
    out_path = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "giga_dropship_push.py",
            "--apply",
            "--db",
            str(db_path),
            "--out",
            str(out_path),
        ],
    )

    assert push.main() == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["results"][0]["status"] == "push_unknown"
    conn = sqlite3.connect(str(db_path))
    plans = plan_dropship_batch(
        [SAMPLE_ORDER],
        skip_already_pushed=True,
        conn=conn,
    )
    assert plans == []
    conn.close()
