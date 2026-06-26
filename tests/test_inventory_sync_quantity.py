from __future__ import annotations

import logging
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_sync_single_product_restocks_with_default_quantity_one():
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service.check_dajian_stock = lambda sku: (True, 120.0, 25.0, 7)
    service.get_last_sync_action = lambda sku: "out_of_stock"
    service._check_and_update_price = lambda *args, **kwargs: None

    seen = {}

    def fake_update_ebay_quantity(sku, quantity):
        seen["sku"] = sku
        seen["quantity"] = quantity
        return True

    service.update_ebay_quantity = fake_update_ebay_quantity

    result = service._sync_single_product(
        {"sku": "SKU-RESTOCK", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )

    assert seen == {"sku": "SKU-RESTOCK", "quantity": 1}
    assert result.action == "restocked"
    assert result.new_value == "库存恢复为1"
    assert "恢复为 1" in result.message


def test_dajian_connection_uses_env_retry_budget_for_transient_dns(monkeypatch):
    import src.plugins.inventory_sync.sync_service as sync_mod
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    class FakeDajianClient:
        def __init__(self):
            self.calls = 0

        def get_product_list(self, page=1, page_size=100):
            self.calls += 1
            if self.calls < 4:
                raise Exception(
                    "Connection Error (after urllib3 retries): "
                    "NameResolutionError: getaddrinfo failed"
                )
            return []

    fake_client = FakeDajianClient()
    sleeps = []
    monkeypatch.setenv("DAJIAN_CONNECT_ATTEMPTS", "4")
    monkeypatch.setenv("DAJIAN_CONNECT_DELAY_SEC", "0.5")
    monkeypatch.setattr(sync_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service._dajian_client = fake_client
    service._get_dajian_client = lambda: fake_client

    assert service.test_dajian_connection() is True
    assert fake_client.calls == 4
    assert sleeps == [0.5, 1.0, 1.5]
    assert service.last_dajian_connection_error == ""


def test_dajian_connection_does_not_long_retry_business_api_error(monkeypatch):
    import src.plugins.inventory_sync.sync_service as sync_mod
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    class FakeDajianClient:
        def __init__(self):
            self.calls = 0

        def get_product_list(self, page=1, page_size=100):
            self.calls += 1
            raise Exception("API Error: Invalid business access")

    fake_client = FakeDajianClient()
    sleeps = []
    monkeypatch.setenv("DAJIAN_CONNECT_ATTEMPTS", "5")
    monkeypatch.setattr(sync_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service._dajian_client = fake_client
    service._get_dajian_client = lambda: fake_client

    assert service.test_dajian_connection() is False
    assert fake_client.calls == 1
    assert sleeps == []
    assert "Invalid business access" in service.last_dajian_connection_error


def test_update_ebay_quantity_requires_exact_live_quantity(monkeypatch):
    import requests
    import src.plugins.inventory_sync.sync_service as sync_mod
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    import src.services.ebay_auth as auth_mod

    class FakeOAuth:
        def __init__(self, *args, **kwargs):
            pass

        def get_valid_token(self):
            return "token"

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    offer_payload = {
        "offers": [
            {
                "offerId": "OFFER-1",
                "status": "PUBLISHED",
                "marketplaceId": "EBAY_US",
                "availableQuantity": 10,
                "listing": {"listingId": "LISTING-1", "listingStatus": "ACTIVE"},
            }
        ]
    }

    def fake_get(url, headers=None, timeout=None, verify=None):
        if "/inventory_item/" in url:
            return FakeResponse(200, {"availability": {"shipToLocationAvailability": {}}})
        return FakeResponse(200, deepcopy(offer_payload))

    def fake_put(url, headers=None, json=None, timeout=None, verify=None):
        return FakeResponse(204)

    monkeypatch.setattr(auth_mod, "EbayOAuthService", FakeOAuth)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr(sync_mod.time, "sleep", lambda *args, **kwargs: None)

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)

    assert service.update_ebay_quantity("SKU-EXACT", 1) is False


def test_update_ebay_quantity_verifies_inventory_item_when_offer_quantity_missing(monkeypatch):
    import requests
    import src.plugins.inventory_sync.sync_service as sync_mod
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    import src.services.ebay_auth as auth_mod

    class FakeOAuth:
        def __init__(self, *args, **kwargs):
            pass

        def get_valid_token(self):
            return "token"

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    state = {"inventory_quantity": 10}
    offer_payload = {
        "offers": [
            {
                "offerId": "OFFER-1",
                "status": "PUBLISHED",
                "marketplaceId": "EBAY_US",
                "listing": {"listingId": "LISTING-1", "listingStatus": "ACTIVE"},
            }
        ]
    }

    def fake_get(url, headers=None, timeout=None, verify=None):
        if "/inventory_item/" in url:
            return FakeResponse(
                200,
                {
                    "availability": {
                        "shipToLocationAvailability": {
                            "quantity": state["inventory_quantity"],
                        }
                    }
                },
            )
        return FakeResponse(200, deepcopy(offer_payload))

    def fake_put(url, headers=None, json=None, timeout=None, verify=None):
        if "/inventory_item/" in url:
            state["inventory_quantity"] = json["availability"]["shipToLocationAvailability"]["quantity"]
        return FakeResponse(204)

    monkeypatch.setattr(auth_mod, "EbayOAuthService", FakeOAuth)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr(sync_mod.time, "sleep", lambda *args, **kwargs: None)

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)

    assert service.update_ebay_quantity("SKU-FALLBACK", 1) is True


def test_update_ebay_quantity_forces_trading_for_out_of_stock_restock(monkeypatch):
    import requests
    import src.plugins.inventory_sync.sync_service as sync_mod
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    import src.services.ebay_auth as auth_mod

    class FakeOAuth:
        def __init__(self, *args, **kwargs):
            pass

        def get_valid_token(self):
            return "token"

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    state = {
        "inventory_quantity": 0,
        "offer_available": 0,
        "listing_status": "OUT_OF_STOCK",
        "trading_calls": [],
    }

    def offer_payload():
        return {
            "offers": [
                {
                    "offerId": "OFFER-RESTOCK",
                    "status": "PUBLISHED",
                    "marketplaceId": "EBAY_US",
                    "availableQuantity": state["offer_available"],
                    "listing": {
                        "listingId": "LISTING-RESTOCK",
                        "listingStatus": state["listing_status"],
                    },
                }
            ]
        }

    def fake_get(url, headers=None, timeout=None, verify=None):
        if "/inventory_item/" in url:
            return FakeResponse(
                200,
                {
                    "availability": {
                        "shipToLocationAvailability": {
                            "quantity": state["inventory_quantity"],
                        }
                    }
                },
            )
        return FakeResponse(200, offer_payload())

    def fake_put(url, headers=None, json=None, timeout=None, verify=None):
        if "/inventory_item/" in url:
            state["inventory_quantity"] = json["availability"]["shipToLocationAvailability"]["quantity"]
        elif "/offer/" in url:
            state["offer_available"] = json["availableQuantity"]
        return FakeResponse(204)

    class FakeEbayClient:
        def __init__(self, *args, **kwargs):
            pass

    class FakeTradingClient:
        def __init__(self, ebay_client):
            pass

        def call(self, call_name, xml_body):
            state["trading_calls"].append((call_name, xml_body))
            state["listing_status"] = "ACTIVE"
            return "<Ack>Success</Ack>"

    monkeypatch.delenv("EBAY_ENABLE_TRADING_QUANTITY_SYNC", raising=False)
    monkeypatch.setattr(auth_mod, "EbayOAuthService", FakeOAuth)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr("src.clients.ebay_client.EbayClient", FakeEbayClient)
    monkeypatch.setattr("src.clients.ebay_trading_client.EbayTradingClient", FakeTradingClient)
    monkeypatch.setattr(sync_mod.time, "sleep", lambda *args, **kwargs: None)

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)

    assert service.update_ebay_quantity("SKU-RESTOCK", 1) is True
    assert state["trading_calls"]
    call_name, xml_body = state["trading_calls"][0]
    assert call_name == "ReviseInventoryStatus"
    assert "<Quantity>1</Quantity>" in xml_body


def test_update_ebay_quantity_accepts_trading_verification_when_sell_status_lags(monkeypatch):
    import requests
    import src.plugins.inventory_sync.sync_service as sync_mod
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    import src.services.ebay_auth as auth_mod

    class FakeOAuth:
        def __init__(self, *args, **kwargs):
            pass

        def get_valid_token(self):
            return "token"

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    state = {"inventory_quantity": 0, "offer_available": 0}

    def fake_get(url, headers=None, timeout=None, verify=None):
        if "/inventory_item/" in url:
            return FakeResponse(
                200,
                {
                    "availability": {
                        "shipToLocationAvailability": {
                            "quantity": state["inventory_quantity"],
                        }
                    }
                },
            )
        return FakeResponse(
            200,
            {
                "offers": [
                    {
                        "offerId": "OFFER-LAG",
                        "status": "PUBLISHED",
                        "marketplaceId": "EBAY_US",
                        "availableQuantity": state["offer_available"],
                        "listing": {
                            "listingId": "LISTING-LAG",
                            "listingStatus": "OUT_OF_STOCK",
                        },
                    }
                ]
            },
        )

    def fake_put(url, headers=None, json=None, timeout=None, verify=None):
        if "/inventory_item/" in url:
            state["inventory_quantity"] = json["availability"]["shipToLocationAvailability"]["quantity"]
        elif "/offer/" in url:
            state["offer_available"] = json["availableQuantity"]
        return FakeResponse(204)

    class FakeEbayClient:
        def __init__(self, *args, **kwargs):
            pass

    class FakeTradingClient:
        def __init__(self, ebay_client):
            pass

        def call(self, call_name, xml_body):
            if call_name == "GetItem":
                return """
                <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
                  <Ack>Success</Ack>
                  <Item>
                    <ItemID>LISTING-LAG</ItemID>
                    <SellingStatus><QuantitySold>1</QuantitySold><ListingStatus>Active</ListingStatus></SellingStatus>
                    <Quantity>2</Quantity>
                  </Item>
                </GetItemResponse>
                """
            return "<Ack>Success</Ack>"

    monkeypatch.delenv("EBAY_ENABLE_TRADING_QUANTITY_SYNC", raising=False)
    monkeypatch.setattr(auth_mod, "EbayOAuthService", FakeOAuth)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr("src.clients.ebay_client.EbayClient", FakeEbayClient)
    monkeypatch.setattr("src.clients.ebay_trading_client.EbayTradingClient", FakeTradingClient)
    monkeypatch.setattr(sync_mod.time, "sleep", lambda *args, **kwargs: None)

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)

    assert service.update_ebay_quantity("SKU-LAG", 1) is True


def test_measurement_fix_prefers_trusted_dajian_dimensions_over_stale_aspects():
    from scripts.fix_measurement_quality_issues import _resolve_dimensions

    assert _resolve_dimensions(
        attrs={},
        aspects={
            "Item Length": ["185 in"],
            "Item Width": ["24 in"],
            "Item Height": ["90 in"],
        },
        trusted={
            "assembledLength": 150.0,
            "assembledWidth": 150.0,
            "assembledHeight": 82.0,
        },
    ) == (150.0, 150.0, 82.0)
