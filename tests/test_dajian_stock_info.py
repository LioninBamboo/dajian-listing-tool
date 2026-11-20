"""get_stock_info keeps list price for the $250 cap and effective promo for cost."""

from __future__ import annotations

from src.clients.dajian_client import DaJianClient
from src.utils.copy_skip_gate import (
    REASON_CARGO_COST,
    apply_live_supplier_quote,
    evaluate_copy_skip,
    lookup_live_supplier_quote,
)
from tests.test_copy_skip_before_llm import CLEAR_DIMS


def _client_with_price(price_info, qty=10):
    client = DaJianClient("k", "s")
    client.get_product_price = lambda sku: price_info
    client.get_inventory_by_sku = lambda sku: {
        "buyerInventoryInfo": {"totalBuyerAvailableInventory": qty}
    }
    return client


def test_get_stock_info_keeps_list_price_and_promo_effective():
    client = _client_with_price(
        {
            "price": 79.90,
            "discountedPrice": 74.90,
            "shippingFee": 10.0,
            "currency": "USD",
            "skuAvailable": True,
        }
    )
    info = client.get_stock_info("W465P539626")
    assert info["price"] == 79.90
    assert info["list_price"] == 79.90
    assert info["discounted_price"] == 74.90
    assert info["effective_price"] == 74.90
    assert info["total_cost"] == 84.90
    assert info["quantity"] == 10
    assert info["in_stock"] is True


def test_get_stock_info_without_promo_uses_list_as_effective():
    client = _client_with_price(
        {"price": 199.0, "shippingFee": 20.0, "skuAvailable": True, "currency": "USD"}
    )
    info = client.get_stock_info("W2700P549534")
    assert info["list_price"] == 199.0
    assert info["effective_price"] == 199.0
    assert info["discounted_price"] is None
    assert info["total_cost"] == 219.0


def test_lookup_quote_costs_on_promo_caps_on_list(monkeypatch):
    def _stock(self, sku):
        return {
            "price": 259.99,
            "list_price": 259.99,
            "discounted_price": 240.0,
            "effective_price": 240.0,
            "shipping_fee": 12.0,
            "currency": "USD",
        }

    monkeypatch.setattr(DaJianClient, "get_stock_info", _stock)
    monkeypatch.setenv("DAJIAN_API_KEY", "k")
    monkeypatch.setenv("DAJIAN_API_SECRET", "s")
    quote = lookup_live_supplier_quote("W3151S00090")
    assert quote["price"] == 240.0
    assert quote["list_price"] == 259.99
    assert quote["shipping"] == 12.0

    class _Product:
        price = 240.0
        shipping = 12.0
        specs = {}

    product = _Product()
    assert apply_live_supplier_quote(product, quote) is True
    assert product.price == 240.0
    assert product.specs["_supplier_list_price"] == 259.99
    decision = evaluate_copy_skip(
        sku="W3151S00090",
        product_price=product.price,
        shipping=product.shipping,
        attributes=dict(CLEAR_DIMS),
        specs=product.specs,
        include_market=False,
    )
    assert REASON_CARGO_COST in decision.reasons


def test_promo_under_cap_with_list_at_250_does_not_skip():
    class _Product:
        price = 199.0
        shipping = 87.14
        specs = {"_supplier_list_price": 250.0}

    product = _Product()
    decision = evaluate_copy_skip(
        sku="W1580S00751",
        product_price=product.price,
        shipping=product.shipping,
        attributes=dict(CLEAR_DIMS),
        specs=product.specs,
        include_market=False,
    )
    assert REASON_CARGO_COST not in decision.reasons
    assert decision.detail["supplier_product_price_usd"] == 250.0


def test_apply_live_supplier_quote_writes_dict_product():
    product = {"price": 29.0, "shipping": 10.0, "specs": {}}
    changed = apply_live_supplier_quote(
        product,
        {"price": 26.1, "list_price": 29.0, "shipping": 18.09},
    )
    assert changed is True
    assert product["price"] == 26.1
    assert product["shipping"] == 18.09
    assert product["specs"]["_supplier_list_price"] == 29.0
