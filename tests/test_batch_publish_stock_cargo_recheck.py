"""Publish-time re-check of live stock and supplier cargo after price refresh."""

from __future__ import annotations

import batch_publish
from src.utils.copy_skip_gate import MAX_SUPPLIER_CARGO_COST_USD, REASON_CARGO_COST


def _product(**overrides):
    product = {
        "sku": "W3204P565677",
        "title": "Coffee Table",
        "description": "<div>Assembly Required Yes</div>",
        "price": 50.0,
        "suggested_price": 99.0,
        "shipping": 10.0,
        "images": ["https://example.com/a.jpg"],
        "attributes": {},
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 40},
        "optimization": {
            "title": "Coffee Table",
            "description": (
                "<div><h3>KEY FEATURES</h3><ul>"
                "<li>Storage shelf keeps living room essentials organized.</li>"
                "<li>Sturdy frame supports everyday coffee table use.</li>"
                "<li>Compact profile fits apartments and family rooms.</li>"
                "</ul></div>"
            ),
            "categoryId": "38204",
            "categoryName": "Coffee Tables",
            "aspects": {"Brand": ["AquaVerve"]},
        },
    }
    product.update(overrides)
    return product


def _stub_publish_preamble(monkeypatch, *, price_side_effect):
    class _FakeOAuth:
        def __init__(self, environment):
            self.environment = environment

        def is_authorized(self):
            return True

    monkeypatch.setattr(batch_publish, "enrich_product_dimensions", lambda product: None)
    monkeypatch.setattr(batch_publish, "validate_product", lambda product: [])
    monkeypatch.setattr(batch_publish, "fetch_market_price", lambda title: None)
    monkeypatch.setattr(batch_publish, "calculate_smart_final_price", price_side_effect)
    monkeypatch.setattr(batch_publish, "record_error", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _FakeOAuth)


def test_publish_blocks_out_of_stock_after_live_refresh(monkeypatch):
    def _price(product, market_p):
        product["live_quantity"] = 0
        product["live_in_stock"] = False
        product["price"] = 50.0
        product["shipping"] = 10.0
        return 99.0

    _stub_publish_preamble(monkeypatch, price_side_effect=_price)

    result = batch_publish.publish_single_product(_product(), dry_run=True)

    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == ["out_of_stock"]
    assert "out_of_stock" in result["message"]


def test_publish_blocks_cargo_cost_over_cap_after_live_refresh(monkeypatch):
    over_cap_price = 240.0
    over_cap_ship = 20.0
    assert over_cap_price + over_cap_ship > MAX_SUPPLIER_CARGO_COST_USD

    def _price(product, market_p):
        product["live_quantity"] = 5
        product["live_in_stock"] = True
        product["price"] = over_cap_price
        product["shipping"] = over_cap_ship
        return 399.0

    _stub_publish_preamble(monkeypatch, price_side_effect=_price)

    result = batch_publish.publish_single_product(_product(), dry_run=True)

    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == [REASON_CARGO_COST]
    assert "cargo_cost_over_cap" in result["message"]


def test_calculate_smart_final_price_stores_live_stock(monkeypatch):
    import sys
    import types

    product = {
        "sku": "W3204P565677",
        "price": 40.0,
        "shipping": 5.0,
        "suggested_price": 0,
        "attributes": {},
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 45.0},
    }

    class _FakeClient:
        def __init__(self, key, secret):
            pass

        def get_stock_info(self, sku):
            return {
                "in_stock": False,
                "quantity": 0,
                "price": 55.0,
                "shipping_fee": 12.0,
            }

    class _PE:
        @staticmethod
        def calculate_selling_price(cost, margin):
            return {"selling_price": round(float(cost) * (1 + margin), 2)}

        @staticmethod
        def calculate_dajian_cost(price, shipping, is_oversize=False):
            return {"total_dajian_cost": float(price) + float(shipping)}

    fake_mod = types.ModuleType("src.clients.dajian_client")
    fake_mod.DaJianClient = _FakeClient
    monkeypatch.setitem(sys.modules, "src.clients.dajian_client", fake_mod)
    monkeypatch.setenv("DAJIAN_API_KEY", "k")
    monkeypatch.setenv("DAJIAN_API_SECRET", "s")
    monkeypatch.setattr(batch_publish, "PricingEngine", _PE)

    final = batch_publish.calculate_smart_final_price(product, market_price=None)

    assert final > 0
    assert product["price"] == 55.0
    assert product["shipping"] == 12.0
    assert product["live_quantity"] == 0
    assert product["live_in_stock"] is False
    assert product["live_supplier_quote_applied"] is True


def _install_fake_dajian(monkeypatch, stock_info):
    import sys
    import types

    class _FakeClient:
        def __init__(self, key, secret):
            pass

        def get_stock_info(self, sku):
            return stock_info

    class _PE:
        @staticmethod
        def calculate_selling_price(cost, margin):
            return {"selling_price": round(float(cost) * (1 + margin), 2)}

        @staticmethod
        def calculate_dajian_cost(price, shipping, is_oversize=False):
            return {"total_dajian_cost": float(price) + float(shipping)}

    fake_mod = types.ModuleType("src.clients.dajian_client")
    fake_mod.DaJianClient = _FakeClient
    monkeypatch.setitem(sys.modules, "src.clients.dajian_client", fake_mod)
    monkeypatch.setenv("DAJIAN_API_KEY", "k")
    monkeypatch.setenv("DAJIAN_API_SECRET", "s")
    monkeypatch.setattr(batch_publish, "PricingEngine", _PE)


def _price_product():
    return {
        "sku": "W3204P565677",
        "price": 40.0,
        "shipping": 5.0,
        "suggested_price": 0,
        "attributes": {},
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 45.0},
    }


def test_failed_price_lookup_is_not_stored_as_out_of_stock(monkeypatch):
    product = _price_product()
    _install_fake_dajian(
        monkeypatch,
        {
            "in_stock": False,
            "quantity": 0,
            "price": 0,
            "error": "Product not found in price API",
            "stock_confirmed": False,
        },
    )

    batch_publish.calculate_smart_final_price(product, market_price=None)

    assert "live_quantity" not in product
    assert "live_in_stock" not in product
    assert product["live_supplier_quote_applied"] is False


def test_unconfirmed_inventory_miss_is_not_stored_as_out_of_stock(monkeypatch):
    product = _price_product()
    _install_fake_dajian(
        monkeypatch,
        {
            "in_stock": False,
            "quantity": 0,
            "price": 55.0,
            "shipping_fee": 12.0,
            "stock_confirmed": False,
        },
    )

    batch_publish.calculate_smart_final_price(product, market_price=None)

    assert product["price"] == 55.0
    assert product["shipping"] == 12.0
    assert "live_quantity" not in product
    assert "live_in_stock" not in product
    assert product["live_supplier_quote_applied"] is True


def test_publish_does_not_block_ebay_listing_price_without_live_quote(monkeypatch):
    def _price(product, market_p):
        product.pop("live_quantity", None)
        product.pop("live_in_stock", None)
        product["live_supplier_quote_applied"] = False
        return 99.0

    _stub_publish_preamble(monkeypatch, price_side_effect=_price)
    product = _product(
        price=400.0,
        shipping=0.0,
        specs={"_price_basis": "ebay_listing"},
        optimization={
            "title": "Coffee Table",
            "description": "short",
            "categoryId": "38204",
            "categoryName": "Coffee Tables",
            "aspects": {"Brand": ["AquaVerve"]},
        },
    )

    result = batch_publish.publish_single_product(product, dry_run=True)

    assert result.get("copy_skip_reasons") != ["cargo_cost_over_cap"]
    assert "cargo_cost_over_cap" not in result["message"]
    assert "placeholder" in result["message"] or "empty" in result["message"].lower()


def test_publish_blocks_ebay_listing_after_live_quote_exceeds_cargo_cap(monkeypatch):
    over_cap_price = 240.0
    over_cap_ship = 20.0

    def _price(product, market_p):
        product["live_quantity"] = 4
        product["live_in_stock"] = True
        product["live_supplier_quote_applied"] = True
        product["price"] = over_cap_price
        product["shipping"] = over_cap_ship
        return 399.0

    _stub_publish_preamble(monkeypatch, price_side_effect=_price)
    product = _product(
        price=400.0,
        shipping=0.0,
        specs={"_price_basis": "ebay_listing"},
    )

    result = batch_publish.publish_single_product(product, dry_run=True)

    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == [REASON_CARGO_COST]


def test_get_stock_info_does_not_confirm_lookup_misses():
    from src.clients.dajian_client import DaJianClient

    client = DaJianClient("k", "s")

    client.get_product_price = lambda sku: None
    missed = client.get_stock_info("SKU")
    assert missed["error"]
    assert missed["stock_confirmed"] is False
    assert missed["quantity"] == 0
    assert missed["in_stock"] is False

    client.get_product_price = lambda sku: {
        "skuAvailable": True,
        "price": 40,
        "shippingFee": 5,
    }
    client.get_inventory_by_sku = lambda sku: None
    empty_inv = client.get_stock_info("SKU")
    assert "error" not in empty_inv
    assert empty_inv["stock_confirmed"] is False
    assert empty_inv["price"] == 40.0

    def _boom(sku):
        raise RuntimeError("inventory timeout")

    client.get_inventory_by_sku = _boom
    raised = client.get_stock_info("SKU")
    assert raised["stock_confirmed"] is False
    assert raised["in_stock"] is False

    client.get_inventory_by_sku = lambda sku: {
        "buyerInventoryInfo": {"totalBuyerAvailableInventory": 0},
        "sellerInventoryInfo": {"sellerAvailableInventory": 0},
    }
    confirmed_zero = client.get_stock_info("SKU")
    assert confirmed_zero["stock_confirmed"] is True
    assert confirmed_zero["quantity"] == 0
    assert confirmed_zero["in_stock"] is False

    client.get_product_price = lambda sku: {
        "skuAvailable": False,
        "price": 40,
        "shippingFee": 5,
    }
    client.get_inventory_by_sku = lambda sku: None
    unavailable = client.get_stock_info("SKU")
    assert unavailable["stock_confirmed"] is True
    assert unavailable["in_stock"] is False
