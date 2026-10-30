from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_sync_single_product_restocks_with_default_quantity_one(monkeypatch):
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")
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


def test_sync_single_product_restocks_with_quantity_cap_two(monkeypatch):
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "2")
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
        {"sku": "SKU-RESTOCK-2", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )

    assert seen == {"sku": "SKU-RESTOCK-2", "quantity": 2}
    assert result.new_value == "库存恢复为2"


def test_sync_single_product_restock_never_exceeds_giga_quantity(monkeypatch):
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "2")
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service.check_dajian_stock = lambda sku: (True, 120.0, 25.0, 1)
    service.get_last_sync_action = lambda sku: "out_of_stock"
    service._check_and_update_price = lambda *args, **kwargs: None
    seen = {}
    service.update_ebay_quantity = lambda sku, quantity: seen.update(sku=sku, quantity=quantity) or True

    result = service._sync_single_product(
        {"sku": "SKU-GIGA-ONE", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )

    assert seen == {"sku": "SKU-GIGA-ONE", "quantity": 1}
    assert result.action == "restocked"
    assert result.new_value == "库存恢复为1"


def test_incomplete_snapshot_extra_row_can_zero_but_not_restock():
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    seen = {}
    service.update_ebay_quantity = lambda sku, quantity: seen.update(sku=sku, quantity=quantity) or True
    service.get_last_sync_action = lambda sku: "out_of_stock"
    service._check_and_update_price = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("price update must not run in zero_only mode")
    )

    service.check_dajian_stock = lambda sku: (False, 120.0, 25.0, 0)
    zeroed = service._sync_single_product(
        {"sku": "MAYBE-LIVE", "cost_breakdown": {}, "sync_mode": "zero_only"},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )
    assert zeroed.action == "out_of_stock"
    assert seen == {"sku": "MAYBE-LIVE", "quantity": 0}

    seen.clear()
    service.check_dajian_stock = lambda sku: (True, 120.0, 25.0, 7)
    skipped = service._sync_single_product(
        {"sku": "MAYBE-LIVE", "cost_breakdown": {}, "sync_mode": "zero_only"},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )
    assert skipped.action == "skipped"
    assert seen == {}


def test_sync_single_product_reports_zero_quantity_write_failure_as_error():
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service.check_dajian_stock = lambda sku: (False, 120.0, 25.0, 0)
    service.update_ebay_quantity = lambda sku, quantity: False

    result = service._sync_single_product(
        {"sku": "SKU-OOS-WRITE-FAIL", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )

    assert result.action == "error"
    assert result.supplier_in_stock is False
    assert "归零失败" in result.message


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


def test_inventory_sync_connect_waits_on_locked_db(tmp_path):
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService.__new__(InventorySyncService)
    service.db_path = str(tmp_path / "sync.db")
    conn = service._connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        conn.close()


def test_record_sync_waits_for_writer_lock_instead_of_skipping_oos(tmp_path):
    """Concurrent scheduler lock must not drop an out-of-stock write."""
    from src.plugins.inventory_sync.sync_service import InventorySyncService, SyncResult

    db_path = str(tmp_path / "sync.db")
    service = InventorySyncService(db_path=db_path)
    ready = threading.Event()

    def _hold_then_release():
        locker = sqlite3.connect(db_path, timeout=0)
        try:
            locker.execute("BEGIN EXCLUSIVE")
            locker.execute("SELECT 1 FROM inventory_sync_log LIMIT 1")
            ready.set()
            time.sleep(0.2)
            locker.commit()
        finally:
            locker.close()

    holder = threading.Thread(target=_hold_then_release)
    holder.start()
    assert ready.wait(timeout=5), "writer lock was never acquired"
    try:
        service.record_sync(SyncResult(
            sku="SKU-LOCKED-OOS",
            action="out_of_stock",
            old_value="有库存",
            new_value="库存设为0",
            message="oos write must wait for lock",
            supplier_in_stock=False,
        ))
    finally:
        holder.join(timeout=10)

    assert service.get_last_sync_action("SKU-LOCKED-OOS") == "out_of_stock"


def _make_collected_db(tmp_path, rows):
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            status TEXT,
            listing_id TEXT,
            title TEXT,
            url TEXT,
            cost_breakdown TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO collected_products
        (sku, status, listing_id, title, url, cost_breakdown)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return db


def test_sync_all_zeroes_live_listing_even_when_db_status_ended(tmp_path):
    """Live on eBay + DB ENDED still belongs in the qty=0 stock gate."""
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    from src.services.live_listing_coverage import LiveListing

    db = _make_collected_db(tmp_path, [
        ("W465P220504", "ENDED", "366701946479", "live drift", "", json.dumps({"base_cost": 10})),
    ])
    service = InventorySyncService(db_path=str(db))
    service._load_favorites_set = lambda: set()
    service._prefetch_dajian_data = lambda skus: None
    service.check_dajian_stock = lambda sku: (False, 10.0, 5.0, 0)
    writes = []
    service.update_ebay_quantity = lambda sku, quantity: writes.append((sku, quantity)) or True

    results = service.sync_all(
        dry_run=False,
        skip_ebay_check=True,
        skip_synced_today=False,
        live_listings=[LiveListing(item_id="366701946479", sku="W465P220504")],
        fetch_complete=True,
    )

    assert writes == [("W465P220504", 0)]
    assert results[0].sku == "W465P220504"
    assert results[0].action == "out_of_stock"
    assert results[0].new_value == "库存设为0"


def test_giga_stock_zero_never_restocks_or_overrides_qty_zero(monkeypatch):
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "2")
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service.check_dajian_stock = lambda sku: (False, 120.0, 25.0, 0)
    service.get_last_sync_action = lambda sku: "out_of_stock"
    service._check_and_update_price = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("price update must not run when GIGA stock is 0")
    )
    writes = []
    service.update_ebay_quantity = lambda sku, quantity: writes.append((sku, quantity)) or True

    result = service._sync_single_product(
        {"sku": "SKU-GIGA-ZERO", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )

    assert writes == [("SKU-GIGA-ZERO", 0)]
    assert result.action == "out_of_stock"
    assert result.new_value == "库存设为0"


def _fake_quantity_http(monkeypatch, state, *, update_offer=True, update_inventory=True):
    import requests
    import src.plugins.inventory_sync.sync_service as sync_mod
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
                        "offerId": "OFFER-SPLIT",
                        "status": "PUBLISHED",
                        "marketplaceId": "EBAY_US",
                        "availableQuantity": state["offer_available"],
                        "listing": {
                            "listingId": "LISTING-SPLIT",
                            "listingStatus": state.get("listing_status", "ACTIVE"),
                        },
                    }
                ]
            },
        )

    def fake_put(url, headers=None, json=None, timeout=None, verify=None):
        if "/inventory_item/" in url and update_inventory:
            state["inventory_quantity"] = json["availability"]["shipToLocationAvailability"]["quantity"]
        elif "/offer/" in url and update_offer:
            state["offer_available"] = json["availableQuantity"]
            if json.get("availableQuantity") == 0:
                state["listing_status"] = "OUT_OF_STOCK"
        return FakeResponse(204)

    monkeypatch.setattr(auth_mod, "EbayOAuthService", FakeOAuth)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr(sync_mod.time, "sleep", lambda *args, **kwargs: None)
    monkeypatch.delenv("EBAY_ENABLE_TRADING_QUANTITY_SYNC", raising=False)


def test_update_ebay_quantity_does_not_push_inventory_item_when_offer_is_lower(monkeypatch):
    """inventory_item=2 / offer=1 must not be treated as live qty 2."""
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    state = {
        "inventory_quantity": 2,
        "offer_available": 1,
        "listing_status": "ACTIVE",
    }
    _fake_quantity_http(monkeypatch, state, update_offer=False, update_inventory=True)

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)

    assert service.update_ebay_quantity("SKU-SPLIT-QTY", 2) is False
    assert state["inventory_quantity"] == 2
    assert state["offer_available"] == 1

    # Writing the offer-visible quantity 1 is allowed; leftover inventory_item
    # 2 must not flip verification over to a live push of 2.
    state["inventory_quantity"] = 2
    state["offer_available"] = 1
    assert service.update_ebay_quantity("SKU-SPLIT-QTY", 1) is True
    assert state["offer_available"] == 1


def test_restock_clamps_to_zero_when_giga_available_is_zero(monkeypatch):
    """in_stock=True with availableQuantity=0 must zero, never write the cap."""
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "2")
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service.check_dajian_stock = lambda sku: (True, 120.0, 25.0, 0)
    service.get_last_sync_action = lambda sku: "out_of_stock"
    service._check_and_update_price = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("price update must not run when restore quantity is 0")
    )
    writes = []
    service.update_ebay_quantity = lambda sku, quantity: writes.append((sku, quantity)) or True

    result = service._sync_single_product(
        {"sku": "SKU-GIGA-AVAIL-ZERO", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )

    assert writes == [("SKU-GIGA-AVAIL-ZERO", 0)]
    assert result.action == "out_of_stock"
    assert "可售数量为 0" in result.message


def test_unknown_dajian_stock_does_not_zero_when_sku_still_in_favorites():
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service.check_dajian_stock = lambda sku: (None, None, None, None)
    service._get_consecutive_skip_count = lambda sku: 1
    writes = []
    service.update_ebay_quantity = lambda sku, quantity: writes.append((sku, quantity)) or True

    result = service._sync_single_product(
        {"sku": "SKU-UNKNOWN", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set={"SKU-UNKNOWN"},
    )

    assert writes == []
    assert result.action == "skipped"
    assert "保持 eBay 库存不变" in result.message


def test_update_ebay_quantity_zero_uses_offer_not_stale_inventory_item(monkeypatch):
    """qty=0 is live when the offer is 0 / OOS, even if inventory_item still says 2."""
    from src.plugins.inventory_sync.sync_service import InventorySyncService

    state = {
        "inventory_quantity": 2,
        "offer_available": 1,
        "listing_status": "ACTIVE",
    }
    _fake_quantity_http(monkeypatch, state, update_offer=True, update_inventory=False)

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)

    assert service.update_ebay_quantity("SKU-ZERO-OFFER", 0) is True
    assert state["offer_available"] == 0
    assert state["listing_status"] == "OUT_OF_STOCK"
    assert state["inventory_quantity"] == 2
