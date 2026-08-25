"""Smart reprice must write Trading listings via Revise, not Inventory offer PUT.

Furniture/main-store defaults stay on Inventory. The Trading write path is
gated on store_profile.listing_channel == "trading" so AquaRides can share
this repo without dumping Motors-only history onto GitHub.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.services.motors_trading import (
    build_revise_fixed_price_item_xml,
    parse_trading_item_price,
)
from src.services.repricing_guard import reprice_write_channel
from src.utils.store_profile import StoreProfile


class _XmlResp:
    def __init__(self, body: str, status_code: int = 200):
        self.status_code = status_code
        self.content = body.encode("utf-8")
        self.text = body


class _JsonResp:
    def __init__(self, payload, status_code: int = 200):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def test_parse_trading_item_price_prefers_current_price():
    payload = (
        "<GetItemResponse><Ack>Success</Ack>"
        "<CurrentPrice currencyID='USD'>102.28</CurrentPrice>"
        "<StartPrice currencyID='USD'>99.00</StartPrice>"
        "</GetItemResponse>"
    )
    assert parse_trading_item_price(payload) == 102.28


def test_parse_trading_item_price_falls_back_to_start_price():
    payload = "<GetItemResponse><StartPrice>88.50</StartPrice></GetItemResponse>"
    assert parse_trading_item_price(payload) == 88.50


def test_price_only_revise_xml_does_not_replace_item_specifics():
    xml = build_revise_fixed_price_item_xml(item_id="188760790738", start_price=56.63)
    assert "<StartPrice>56.63</StartPrice>" in xml
    assert "<ItemSpecifics>" not in xml
    assert "<Title>" not in xml
    assert "<Description>" not in xml
    assert "<ItemCompatibilityList>" not in xml


def test_default_store_reprice_channel_is_inventory():
    assert reprice_write_channel("188760790738", StoreProfile()) == "inventory"
    assert reprice_write_channel(None, StoreProfile()) == "inventory"


def test_trading_store_reprice_channel_is_trading():
    profile = StoreProfile(listing_channel="trading", ebay_site_id="100")
    assert reprice_write_channel("188760790738", profile) == "trading"


def test_trading_store_can_keep_named_leftovers_on_inventory(monkeypatch):
    profile = StoreProfile(listing_channel="trading", ebay_site_id="100")
    monkeypatch.setenv("REPRICE_INVENTORY_LISTING_IDS", "188683585596")
    assert reprice_write_channel("188683585596", profile) == "inventory"
    assert reprice_write_channel("188760790738", profile) == "trading"


def test_update_ebay_price_uses_trading_revise_when_channel_is_trading():
    from scripts import batch_smart_reprice as reprice

    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"
    profile = StoreProfile(
        listing_channel="trading",
        ebay_site_id="100",
        brand_name="AquaRides",
    )
    revise_ok = _XmlResp(
        "<ReviseFixedPriceItemResponse><Ack>Success</Ack></ReviseFixedPriceItemResponse>"
    )

    with patch(
        "src.services.repricing_guard.precheck_price", return_value=(True, "ok")
    ), patch(
        "src.utils.store_profile.get_store_profile", return_value=profile
    ), patch.object(
        reprice.requests, "post", return_value=revise_ok
    ) as post, patch.object(
        reprice.requests, "get"
    ) as get, patch.object(
        reprice.requests, "put"
    ) as put:
        ok = reprice.update_ebay_price(
            oauth, "W465P475235", 56.63, expected_listing_id="188760790738"
        )

    assert ok is True
    post.assert_called_once()
    headers = post.call_args.kwargs["headers"]
    assert headers["X-EBAY-API-CALL-NAME"] == "ReviseFixedPriceItem"
    assert headers["X-EBAY-API-SITEID"] == "100"
    body = post.call_args.kwargs["data"].decode("utf-8")
    assert "<StartPrice>56.63</StartPrice>" in body
    assert "<ItemSpecifics>" not in body
    get.assert_not_called()
    put.assert_not_called()


def test_update_ebay_price_treats_trading_warning_ack_as_success():
    from scripts import batch_smart_reprice as reprice

    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"
    profile = StoreProfile(listing_channel="trading", ebay_site_id="100")
    revise_warn = _XmlResp(
        "<ReviseFixedPriceItemResponse><Ack>Warning</Ack></ReviseFixedPriceItemResponse>"
    )

    with patch(
        "src.services.repricing_guard.precheck_price", return_value=(True, "ok")
    ), patch(
        "src.utils.store_profile.get_store_profile", return_value=profile
    ), patch.object(
        reprice.requests, "post", return_value=revise_warn
    ):
        ok = reprice.update_ebay_price(
            oauth, "W465P475235", 56.63, expected_listing_id="188760790738"
        )

    assert ok is True


def test_update_ebay_price_uses_inventory_put_on_main_store():
    from scripts import batch_smart_reprice as reprice

    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"
    get_resp = _JsonResp(
        {
            "offers": [
                {
                    "offerId": "off-1",
                    "status": "PUBLISHED",
                    "marketplaceId": "EBAY_US",
                    "listing": {"listingId": "123", "listingStatus": "ACTIVE"},
                    "pricingSummary": {"price": {"value": "10.00"}},
                }
            ]
        }
    )
    put_resp = _JsonResp({}, status_code=204)

    with patch(
        "src.services.repricing_guard.precheck_price", return_value=(True, "ok")
    ), patch(
        "src.utils.store_profile.get_store_profile", return_value=StoreProfile()
    ), patch.object(
        reprice.requests, "post"
    ) as post, patch.object(
        reprice.requests, "get", return_value=get_resp
    ) as get, patch.object(
        reprice.requests, "put", return_value=put_resp
    ) as put:
        ok = reprice.update_ebay_price(
            oauth, "SKU1", 12.34, expected_listing_id="123"
        )

    assert ok is True
    get.assert_called()
    put.assert_called()
    post.assert_not_called()
    offer = put.call_args.kwargs.get("json") or put.call_args[1].get("json")
    assert offer["pricingSummary"]["price"]["value"] == "12.34"


def test_fetch_live_offer_price_uses_getitem_on_trading_channel():
    from scripts import batch_smart_reprice as reprice

    oauth = MagicMock()
    oauth.get_valid_token.return_value = "tok"
    profile = StoreProfile(listing_channel="trading", ebay_site_id="100")
    getitem = _XmlResp(
        "<GetItemResponse><Ack>Success</Ack>"
        "<CurrentPrice currencyID='USD'>56.63</CurrentPrice>"
        "</GetItemResponse>"
    )

    with patch(
        "src.utils.store_profile.get_store_profile", return_value=profile
    ), patch.object(
        reprice.requests, "post", return_value=getitem
    ) as post, patch.object(
        reprice.requests, "get"
    ) as get:
        price = reprice.fetch_live_offer_price(
            oauth, "W465P475235", expected_listing_id="188760790738"
        )

    assert price == 56.63
    post.assert_called_once()
    assert post.call_args.kwargs["headers"]["X-EBAY-API-CALL-NAME"] == "GetItem"
    get.assert_not_called()
