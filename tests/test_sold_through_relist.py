"""Sold-through Motors/Trading restock: relist, Trading fallback, no false DELISTED."""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SKU = "W2680P518198"
OLD_ITEM = "189043406317"
NEW_ITEM = "189063211766"


def _completed_item_xml(item_id: str = OLD_ITEM, sku: str = SKU) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Item>
        <ItemID>{item_id}</ItemID>
        <SKU>{sku}</SKU>
        <Quantity>1</Quantity>
        <QuantityAvailable>0</QuantityAvailable>
        <ListingDetails><EndingReason>Sold</EndingReason></ListingDetails>
        <SellingStatus>
          <QuantitySold>1</QuantitySold>
          <ListingStatus>Completed</ListingStatus>
        </SellingStatus>
      </Item>
    </GetItemResponse>
    """


def _active_item_xml(item_id: str, sku: str, available: int) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Item>
        <ItemID>{item_id}</ItemID>
        <SKU>{sku}</SKU>
        <Quantity>{available}</Quantity>
        <QuantityAvailable>{available}</QuantityAvailable>
        <SellingStatus>
          <QuantitySold>0</QuantitySold>
          <ListingStatus>Active</ListingStatus>
        </SellingStatus>
      </Item>
    </GetItemResponse>
    """


def _init_db(path: Path, sku: str = SKU, listing_id: str = OLD_ITEM, status: str = "PUBLISHED") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT,
            listing_id TEXT,
            status TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO collected_products (sku, listing_id, status) VALUES (?, ?, ?)",
        (sku, listing_id, status),
    )
    conn.commit()
    conn.close()


def _patch_oauth_and_http(monkeypatch, status_code: int = 404):
    import requests
    import src.plugins.inventory_sync.sync_service as sync_mod
    import src.services.ebay_auth as auth_mod

    class FakeOAuth:
        def __init__(self, *args, **kwargs):
            pass

        def get_valid_token(self):
            return "token"

    class FakeResponse:
        def __init__(self, code):
            self.status_code = code
            self.text = "NOT_FOUND"

        def json(self):
            return {}

    monkeypatch.setattr(auth_mod, "EbayOAuthService", FakeOAuth)
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: FakeResponse(status_code))
    monkeypatch.setattr(sync_mod.time, "sleep", lambda *args, **kwargs: None)


def _patch_trading(monkeypatch, call):
    class FakeEbayClient:
        def __init__(self, *args, **kwargs):
            pass

    class FakeTradingClient:
        def __init__(self, ebay_client):
            pass

        def call(self, call_name, xml_body, site_id=None):
            return call(call_name, xml_body, site_id)

    monkeypatch.setattr("src.clients.ebay_client.EbayClient", FakeEbayClient)
    monkeypatch.setattr("src.clients.ebay_trading_client.EbayTradingClient", FakeTradingClient)
    monkeypatch.setattr(
        "src.plugins.inventory_sync.sync_service.trading_site_id",
        lambda: "100",
    )


def _checker(db_path: Path):
    from scripts.sales_health_check import SalesHealthChecker

    checker = SalesHealthChecker()
    checker.db_path = db_path
    return checker


def _patch_supplier_gate(monkeypatch):
    monkeypatch.setenv("DAJIAN_API_KEY", "test-key")
    monkeypatch.setenv("DAJIAN_API_SECRET", "test-secret")
    monkeypatch.setattr("src.clients.dajian_client.DaJianClient", lambda *args, **kwargs: object())


def test_completed_sold_with_supplier_stock_relists_on_motors_site(monkeypatch, tmp_path):
    db_path = tmp_path / "ebay.db"
    _init_db(db_path)
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "2")
    _patch_oauth_and_http(monkeypatch)
    _patch_supplier_gate(monkeypatch)
    monkeypatch.setattr("scripts.sales_health_check.uses_trading_channel", lambda: True)

    calls = []

    def trading_call(call_name, xml_body, site_id):
        calls.append((call_name, xml_body, site_id))
        if call_name == "RelistFixedPriceItem":
            return (
                "<RelistFixedPriceItemResponse><Ack>Success</Ack>"
                f"<ItemID>{NEW_ITEM}</ItemID></RelistFixedPriceItemResponse>"
            )
        if call_name == "GetItem" and NEW_ITEM in xml_body:
            return _active_item_xml(NEW_ITEM, SKU, 2)
        return _completed_item_xml()

    _patch_trading(monkeypatch, trading_call)

    from src.plugins.inventory_sync.sync_service import InventorySyncService

    monkeypatch.setattr(
        InventorySyncService,
        "check_dajian_stock",
        lambda self, sku: (True, 40.0, 5.0, 99),
    )

    checker = _checker(db_path)
    monkeypatch.setattr(checker, "_fetch_trading_active_quantity_map", lambda: {})
    monkeypatch.setattr(
        checker,
        "_fetch_trading_recent_sold_map",
        lambda: ({
            SKU: {
                "listing_id": OLD_ITEM,
                "available": 0,
                "quantity": 1,
                "sold": 1,
                "listing_status": "Completed",
                "ending_reason": "Sold",
            }
        }, {}),
    )
    monkeypatch.setattr(
        checker,
        "_fetch_trading_listing_snapshot",
        lambda listing_id: {
            "item_id": OLD_ITEM,
            "sku": SKU,
            "listing_status": "Completed",
            "ending_reason": "Sold",
            "quantity_sold": 1,
            "available": 0,
        },
    )

    audit = checker._check_quantity_integrity(auto_fix=True)

    relist_calls = [call for call in calls if call[0] == "RelistFixedPriceItem"]
    assert len(relist_calls) == 1
    _name, body, site_id = relist_calls[0]
    assert site_id == "100"
    assert f"<ItemID>{OLD_ITEM}</ItemID>" in body
    assert "<Quantity>2</Quantity>" in body
    assert f"<SKU>{SKU}</SKU>" in body
    assert "<ItemCompatibilityList>" not in body
    assert "<ItemSpecifics>" not in body
    assert "Item Length" not in body
    assert all(call[2] == "100" for call in calls)

    conn = sqlite3.connect(db_path)
    listing_id, status = conn.execute(
        "SELECT listing_id, status FROM collected_products WHERE sku = ?",
        (SKU,),
    ).fetchone()
    action, new_value = conn.execute(
        "SELECT action, new_value FROM inventory_sync_log WHERE sku = ?",
        (SKU,),
    ).fetchone()
    conn.close()

    assert listing_id == NEW_ITEM
    assert status == "PUBLISHED"
    assert action == "restocked"
    assert new_value == "库存恢复为2"
    assert audit["restocked_count"] == 1
    assert audit["supplier_oos_count"] == 0


def test_offer_not_found_falls_back_to_trading_revise(monkeypatch, tmp_path):
    db_path = tmp_path / "ebay.db"
    _init_db(db_path, sku="SKU-ACTIVE", listing_id="LISTING-ACTIVE")
    _patch_oauth_and_http(monkeypatch, status_code=404)

    calls = []
    state = {"revised": False}

    def trading_call(call_name, xml_body, site_id):
        calls.append((call_name, xml_body, site_id))
        if call_name == "ReviseInventoryStatus":
            state["revised"] = True
            return "<Ack>Success</Ack>"
        available = 2 if state["revised"] else 0
        return _active_item_xml("LISTING-ACTIVE", "SKU-ACTIVE", available)

    _patch_trading(monkeypatch, trading_call)

    from src.plugins.inventory_sync.sync_service import InventorySyncService

    service = InventorySyncService(db_path=str(db_path))
    service.logger = logging.getLogger(__name__)

    assert service.update_ebay_quantity("SKU-ACTIVE", 2, known_listing_id="LISTING-ACTIVE") is True
    revise_calls = [call for call in calls if call[0] == "ReviseInventoryStatus"]
    assert len(revise_calls) == 1
    assert revise_calls[0][2] == "100"
    assert "<Quantity>2</Quantity>" in revise_calls[0][1]
    assert "<SKU>SKU-ACTIVE</SKU>" in revise_calls[0][1]
    assert not any(call[0] == "RelistFixedPriceItem" for call in calls)
    assert not any(call[0].startswith("End") for call in calls)

    conn = sqlite3.connect(db_path)
    status = conn.execute(
        "SELECT status FROM collected_products WHERE sku = ?",
        ("SKU-ACTIVE",),
    ).fetchone()[0]
    conn.close()
    assert status == "PUBLISHED"


def test_supplier_oos_does_not_relist(monkeypatch, tmp_path):
    db_path = tmp_path / "ebay.db"
    _init_db(db_path)
    _patch_oauth_and_http(monkeypatch)
    _patch_supplier_gate(monkeypatch)
    monkeypatch.setattr("scripts.sales_health_check.uses_trading_channel", lambda: True)

    from src.plugins.inventory_sync.sync_service import InventorySyncService

    calls = []

    def spy_update(self, sku, quantity, known_listing_id=None):
        calls.append((sku, quantity, known_listing_id))
        return True

    monkeypatch.setattr(InventorySyncService, "update_ebay_quantity", spy_update)
    monkeypatch.setattr(
        InventorySyncService,
        "check_dajian_stock",
        lambda self, sku: (False, 40.0, 5.0, 0),
    )
    monkeypatch.setattr(
        InventorySyncService,
        "check_ebay_listing_status",
        lambda self, sku: "NOT_FOUND",
    )

    checker = _checker(db_path)
    monkeypatch.setattr(checker, "_fetch_trading_active_quantity_map", lambda: {})
    monkeypatch.setattr(
        checker,
        "_fetch_trading_recent_sold_map",
        lambda: ({
            SKU: {
                "listing_id": OLD_ITEM,
                "available": 0,
                "sold": 1,
                "listing_status": "Completed",
                "ending_reason": "Sold",
            }
        }, {}),
    )
    monkeypatch.setattr(
        checker,
        "_fetch_trading_listing_snapshot",
        lambda listing_id: {
            "listing_status": "Completed",
            "ending_reason": "Sold",
            "quantity_sold": 1,
            "item_id": OLD_ITEM,
        },
    )

    audit = checker._check_quantity_integrity(auto_fix=True)

    assert calls == []
    assert audit["restocked_count"] == 0
    assert audit["supplier_oos_count"] == 1
    conn = sqlite3.connect(db_path)
    listing_id, status = conn.execute(
        "SELECT listing_id, status FROM collected_products WHERE sku = ?",
        (SKU,),
    ).fetchone()
    logged = conn.execute(
        "SELECT COUNT(*) FROM inventory_sync_log WHERE sku = ? AND action = 'restocked'",
        (SKU,),
    ).fetchone()[0]
    conn.close()
    assert listing_id == OLD_ITEM
    assert status == "PUBLISHED"
    assert logged == 0


def test_trading_not_found_does_not_mark_delisted(monkeypatch, tmp_path):
    db_path = tmp_path / "ebay.db"
    _init_db(db_path, listing_id="LISTING-MISSING")
    _patch_oauth_and_http(monkeypatch)
    _patch_supplier_gate(monkeypatch)

    from src.plugins.inventory_sync.sync_service import InventorySyncService

    calls = []
    monkeypatch.setattr(
        InventorySyncService,
        "update_ebay_quantity",
        lambda self, sku, quantity, known_listing_id=None: calls.append(sku) or True,
    )
    monkeypatch.setattr(
        InventorySyncService,
        "check_ebay_listing_status",
        lambda self, sku: "NOT_FOUND",
    )

    checker = _checker(db_path)
    monkeypatch.setattr(
        checker,
        "_fetch_trading_active_quantity_map",
        lambda: {
            SKU: {"available": 0, "quantity": 1, "sold": 1, "listing_status": "Active"},
        },
    )
    monkeypatch.setattr(
        checker,
        "_fetch_trading_listing_snapshot",
        lambda listing_id: {"listing_status": "NOT_FOUND"},
    )

    audit = checker._check_quantity_integrity(auto_fix=True)

    conn = sqlite3.connect(db_path)
    status = conn.execute(
        "SELECT status FROM collected_products WHERE sku = ?",
        (SKU,),
    ).fetchone()[0]
    conn.close()
    assert status == "PUBLISHED"
    assert calls == []
    assert audit["restocked_count"] == 0


def test_sold_list_scan_keeps_completed_sold_on_site_100(monkeypatch):
    from scripts.sales_health_check import SalesHealthChecker

    sold_xml = """<?xml version="1.0" encoding="utf-8"?>
    <GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <SoldList>
        <PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult>
        <OrderTransactionArray>
          <OrderTransaction>
            <Transaction>
              <Item>
                <ItemID>189043406317</ItemID>
                <SKU>W2680P518198</SKU>
                <Quantity>1</Quantity>
                <SellingStatus>
                  <QuantitySold>1</QuantitySold>
                  <ListingStatus>Completed</ListingStatus>
                </SellingStatus>
                <ListingDetails><EndingReason>Sold</EndingReason></ListingDetails>
              </Item>
              <QuantityPurchased>1</QuantityPurchased>
            </Transaction>
          </OrderTransaction>
          <OrderTransaction>
            <Transaction>
              <Item>
                <ItemID>111</ItemID>
                <SKU>SELLER-ENDED</SKU>
                <Quantity>1</Quantity>
                <SellingStatus>
                  <QuantitySold>1</QuantitySold>
                  <ListingStatus>Completed</ListingStatus>
                </SellingStatus>
                <ListingDetails><EndingReason>NotAvailable</EndingReason></ListingDetails>
              </Item>
            </Transaction>
          </OrderTransaction>
        </OrderTransactionArray>
      </SoldList>
    </GetMyeBaySellingResponse>
    """
    seen = {}

    class FakeEbayClient:
        def __init__(self, *args, **kwargs):
            pass

    class FakeTradingClient:
        def __init__(self, ebay_client):
            pass

        def get_sold_listings(self, page=1, limit=200, duration_days=60, site_id=None):
            seen["site_id"] = site_id
            seen["duration_days"] = duration_days
            return sold_xml

    monkeypatch.setattr("src.clients.ebay_client.EbayClient", FakeEbayClient)
    monkeypatch.setattr("src.clients.ebay_trading_client.EbayTradingClient", FakeTradingClient)
    monkeypatch.setattr("scripts.sales_health_check.trading_site_id", lambda: "100")

    by_sku, by_item = SalesHealthChecker()._fetch_trading_recent_sold_map()

    assert seen["site_id"] == "100"
    assert SKU in by_sku
    assert by_sku[SKU]["listing_id"] == OLD_ITEM
    assert by_sku[SKU]["ending_reason"] == "Sold"
    assert "SELLER-ENDED" not in by_sku
    assert OLD_ITEM in by_item
    assert "111" not in by_item
