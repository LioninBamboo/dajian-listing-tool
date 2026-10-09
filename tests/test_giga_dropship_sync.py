"""Phase 2: GIGA track → eBay shipping fulfillment helpers."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from src.services.giga_dropship_sync import (
    create_ebay_shipping_fulfillment,
    extract_tracking_entries,
    list_pending_fulfillment_rows,
    map_carrier_code,
    sync_one_fulfillment_row,
    _line_items_for_tracking,
)
from src.services.giga_dropship import (
    ensure_fulfillment_table,
    get_fulfillment_row,
    record_fulfillment_attempt,
)


def test_map_carrier_code():
    assert map_carrier_code("FedEx") == "FedEx"
    assert map_carrier_code("UPS Ground") == "UPS"
    assert map_carrier_code("Some Local Courier") == "Other"


def test_extract_tracking_entries():
    rows = [
        {
            "orderNo": "EB1",
            "shipTrackInfo": [
                {
                    "sku": "W1",
                    "skuQty": 1,
                    "trackingNum": "79489124329",
                    "carrierName": "FedEx",
                    "shipFromInfo": {"warehouseCode": "CA3", "country": "US"},
                },
                {"sku": "W1", "trackingNum": "", "carrierName": "FedEx"},
            ],
        }
    ]
    tracks = extract_tracking_entries(rows)
    assert len(tracks) == 1
    assert tracks[0]["trackingNum"] == "79489124329"
    assert tracks[0]["warehouseCode"] == "CA3"


def test_line_items_for_tracking_matches_sku():
    order = {
        "lineItems": [
            {
                "lineItemId": "111",
                "sku": "W1",
                "quantity": 1,
                "lineItemFulfillmentStatus": "NOT_STARTED",
            },
            {
                "lineItemId": "222",
                "sku": "W2",
                "quantity": 1,
                "lineItemFulfillmentStatus": "NOT_STARTED",
            },
        ]
    }
    lines = _line_items_for_tracking(order, [{"sku": "W1", "trackingNum": "T"}])
    assert lines == [{"lineItemId": "111", "quantity": 1}]


def test_line_items_for_tracking_does_not_guess_across_multiple_open_lines():
    order = {
        "lineItems": [
            {"lineItemId": "111", "sku": "W1", "quantity": 1},
            {"lineItemId": "222", "sku": "W2", "quantity": 1},
        ]
    }

    lines = _line_items_for_tracking(
        order,
        [{"sku": "UNKNOWN", "trackingNum": "T"}],
    )

    assert lines == []


def test_line_items_for_tracking_respects_partial_tracking_quantity():
    order = {
        "lineItems": [
            {"lineItemId": "111", "sku": "W1", "quantity": 3},
        ]
    }

    lines = _line_items_for_tracking(
        order,
        [{"sku": "W1", "skuQty": 1, "trackingNum": "T"}],
    )

    assert lines == [{"lineItemId": "111", "quantity": 1}]


def test_create_ebay_shipping_fulfillment_dry_run():
    out = create_ebay_shipping_fulfillment(
        "04-1",
        line_items=[{"lineItemId": "100", "quantity": 1}],
        tracking_number="T123",
        shipping_carrier_code="FedEx",
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["body"]["trackingNumber"] == "T123"
    assert out["body"]["shippingCarrierCode"] == "FedEx"


def test_sync_one_still_processing_without_track(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    record_fulfillment_attempt(
        conn,
        ebay_order_id="OID1",
        giga_order_no="EBOID1",
        status="pushed",
        payload={"orderNo": "EBOID1"},
    )
    row = list_pending_fulfillment_rows(conn, ebay_order_id="OID1")[0]

    dajian = MagicMock()
    dajian.query_order_status.return_value = [{"orderNo": "EBOID1", "orderStatus": "2"}]
    dajian.query_order_tracking.return_value = [{"orderNo": "EBOID1", "shipTrackInfo": []}]

    out = sync_one_fulfillment_row(row, dajian_client=dajian, dry_run=True, conn=conn)
    assert out["status"] == "still_processing"
    conn.close()


def test_sync_one_ships_when_track_present(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    record_fulfillment_attempt(
        conn,
        ebay_order_id="OID2",
        giga_order_no="EBOID2",
        status="pushed",
        payload={"orderNo": "EBOID2"},
    )
    row = list_pending_fulfillment_rows(conn, ebay_order_id="OID2")[0]

    dajian = MagicMock()
    dajian.query_order_status.return_value = [{"orderNo": "EBOID2", "orderStatus": "20"}]
    dajian.query_order_tracking.return_value = [
        {
            "orderNo": "EBOID2",
            "shipTrackInfo": [
                {"sku": "W1", "trackingNum": "TRACK1", "carrierName": "FedEx"}
            ],
        }
    ]

    monkeypatch.setattr(
        "src.services.giga_dropship_sync.get_ebay_order",
        lambda oid, oauth=None: {
            "orderId": oid,
            "orderFulfillmentStatus": "NOT_STARTED",
            "lineItems": [
                {
                    "lineItemId": "LI1",
                    "sku": "W1",
                    "quantity": 1,
                    "lineItemFulfillmentStatus": "NOT_STARTED",
                }
            ],
        },
    )

    out = sync_one_fulfillment_row(row, dajian_client=dajian, dry_run=True, conn=conn)
    assert out["status"] == "shipped_dry_run"
    assert out["ebay_fulfill"]["body"]["trackingNumber"] == "TRACK1"
    assert out["ebay_fulfill"]["body"]["lineItems"][0]["lineItemId"] == "LI1"
    conn.close()


def test_sync_one_fails_closed_for_multiple_tracking_numbers(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    record_fulfillment_attempt(
        conn,
        ebay_order_id="OID3",
        giga_order_no="EBOID3",
        status="pushed",
        payload={"orderNo": "EBOID3"},
    )
    row = list_pending_fulfillment_rows(conn, ebay_order_id="OID3")[0]
    dajian = MagicMock()
    dajian.query_order_status.return_value = [{"orderNo": "EBOID3", "orderStatus": "20"}]
    dajian.query_order_tracking.return_value = [{
        "orderNo": "EBOID3",
        "shipTrackInfo": [
            {"sku": "W1", "trackingNum": "TRACK1", "carrierName": "FedEx"},
            {"sku": "W2", "trackingNum": "TRACK2", "carrierName": "FedEx"},
        ],
    }]
    monkeypatch.setattr(
        "src.services.giga_dropship_sync.get_ebay_order",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not fetch/write eBay for unsupported multi-package data")
        ),
    )

    out = sync_one_fulfillment_row(row, dajian_client=dajian, dry_run=False, conn=conn)

    assert out["status"] == "multiple_tracking_numbers_unsupported"
    assert get_fulfillment_row(conn, "OID3")["status"] == "manual_review"
    conn.close()


def test_pending_rows_include_sync_retry_states(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    for index, status in enumerate(("fulfill_failed", "sync_error"), start=1):
        record_fulfillment_attempt(
            conn,
            ebay_order_id=f"OID{index}",
            giga_order_no=f"EBOID{index}",
            status=status,
            payload={"orderNo": f"EBOID{index}"},
        )

    rows = list_pending_fulfillment_rows(conn)

    assert {row["status"] for row in rows} == {"fulfill_failed", "sync_error"}
    conn.close()
