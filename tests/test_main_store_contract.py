"""Main-store (furniture / AquaVerve) contract tests — machine-enforced isolation.

Everything the sub-stores (blind box, auto parts) add is gated so the main store
never enters the new code paths. These tests pin that guarantee: if a change
alters the main store's zero-config defaults or its Inventory-API publish payload,
they fail. Treat a failure here as "you touched the main store" — not as a test to
update, unless the main store's behavior is *intentionally* changing.

Two layers:
  A. profile-defaults contract — StoreProfile() with no config == main store.
  B. publish-payload contract — create_inventory_item / create_offer produce the
     standard EBAY_US Inventory payload with no Motors/Trading leakage.
"""

import json

import pytest

from src.utils.store_profile import StoreProfile
from src.clients.real_ebay_client import RealEbayClient


# --- Layer A: zero-config defaults must equal the main store -------------------


class TestProfileDefaultsContract:
    def test_defaults_are_main_store(self):
        p = StoreProfile()  # no YAML, no env — the guaranteed floor
        # identity
        assert p.brand_name == "AquaVerve"
        assert p.store_kind == "furniture"
        assert p.merchant_location_key == "DAJIAN_LA_WAREHOUSE"
        # marketplace / catalog: general eBay.com US, NOT Motors
        assert p.ebay_marketplace_id == "EBAY_US"
        assert p.category_tree_id == "0"
        assert p.is_motors is False
        # listing generation: furniture template, house brand stamped
        assert p.template_style == "furniture_classic"
        assert p.force_house_brand is True
        assert p.default_brand == "AquaVerve"
        assert p.banned_terms == ()          # guard is a no-op for main
        # pricing: cost-plus, not the blind-box undercut model
        assert p.pricing_strategy == "cost_plus"
        assert p.price_ends_99 is False
        # server
        assert p.server_port == 8000

    def test_channel_defaults_to_inventory(self):
        # Guards the P0-A auto-parts Trading channel: whatever field name gates it
        # (listing_channel / ebay_site_id), the main store's zero-config value must
        # keep it on the existing Inventory path. Update the expected value here
        # ONLY when the field is added, never loosen it to a truthy default.
        p = StoreProfile()
        assert p.listing_channel == "inventory"   # main uses the Inventory API
        assert p.ebay_site_id == "0"              # eBay.com US, not Motors (100)

    def test_no_field_default_is_motors_or_trading(self):
        # A blunt catch-all: no default value should smell like Motors/Trading.
        p = StoreProfile()
        blob = json.dumps({f: getattr(p, f) for f in vars(p)}, default=str).lower()
        assert "motors" not in blob
        assert "trading" not in blob
        assert "100" not in str(p.category_tree_id)


# --- Layer B: main-store publish payload (Inventory API, EBAY_US) --------------


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _CaptureSession:
    def __init__(self):
        self.put_calls = []
        self.post_calls = []

    def put(self, url, headers=None, json=None, **kw):
        self.put_calls.append({"url": url, "json": json})
        return _Resp(204)

    def post(self, url, headers=None, json=None, **kw):
        self.post_calls.append({"url": url, "json": json})
        return _Resp(201, {"offerId": "offer-contract-1"})

    def get(self, *a, **kw):
        return _Resp(200, {})


class _DummyOauth:
    def get_valid_token(self):
        return "token-contract"

    def get_application_token(self):
        return "app-token-contract"


def _contract_client(monkeypatch):
    # Force the main-store profile everywhere the client reads it.
    from src.utils import store_profile as sp

    monkeypatch.setattr(sp, "get_store_profile", lambda: StoreProfile())
    monkeypatch.setattr("src.clients.real_ebay_client.get_store_profile", lambda: StoreProfile())

    client = object.__new__(RealEbayClient)
    client.base_url = "https://api.ebay.example"
    client.oauth = _DummyOauth()
    client.session = _CaptureSession()
    client.marketplace_id = StoreProfile().ebay_marketplace_id
    client._complete_listing_policies = lambda policies=None: {
        "fulfillmentPolicyId": "321897899021",
        "returnPolicyId": "321896608021",
        "paymentPolicyId": "321896606021",
    }
    return client


_FURNITURE = {
    "title": "AquaVerve 3-Seat Sectional Sofa with Ottoman",
    "description": "<div>Comfortable sectional sofa. Solid wood frame.</div>",
    # eBay-hosted URL so image prep reuses it as-is (no network).
    "image_urls": ["https://i.ebayimg.com/images/g/abc/s-l1600.jpg"],
    "price": 599.0,
    "quantity": 10,
    "condition": "NEW",
    "aspects": {"Brand": ["AquaVerve"], "Type": ["Sectional Sofa"], "Material": ["Fabric"]},
}


class TestOfferPayloadContract:
    def test_offer_is_inventory_ebay_us(self, monkeypatch):
        c = _contract_client(monkeypatch)
        c.create_offer("SKU-FURN-1", 599.0, category_id="38208",
                       listing_description="<div>desc</div>")
        body = c.session.post_calls[0]["json"]
        assert body["marketplaceId"] == "EBAY_US"
        assert body["format"] == "FIXED_PRICE"
        assert body["merchantLocationKey"] == "DAJIAN_LA_WAREHOUSE"
        assert body["categoryId"] == "38208"
        assert body["pricingSummary"]["price"] == {"value": "599.0", "currency": "USD"}
        assert body["listingPolicies"]["fulfillmentPolicyId"] == "321897899021"

    def test_offer_has_no_motors_or_compatibility_leakage(self, monkeypatch):
        c = _contract_client(monkeypatch)
        c.create_offer("SKU-FURN-1", 599.0, category_id="38208")
        blob = json.dumps(c.session.post_calls[0]["json"]).lower()
        assert "compat" not in blob
        assert "motors" not in blob
        assert "siteid" not in blob


class TestInventoryItemPayloadContract:
    def test_inventory_item_shape_is_stable(self, monkeypatch):
        c = _contract_client(monkeypatch)
        c.create_or_replace_inventory_item("SKU-FURN-1", _FURNITURE)
        body = c.session.put_calls[-1]["json"]
        assert body["condition"] == "NEW"
        assert body["availability"]["shipToLocationAvailability"]["quantity"] == 10
        prod = body["product"]
        assert prod["title"] and prod["description"]
        assert prod["imageUrls"] == _FURNITURE["image_urls"]
        assert isinstance(prod["aspects"], dict)
        # house brand stamped for furniture
        assert prod.get("brand") == "AquaVerve"

    def test_inventory_item_has_no_compatibility(self, monkeypatch):
        c = _contract_client(monkeypatch)
        c.create_or_replace_inventory_item("SKU-FURN-1", _FURNITURE)
        assert "compatibility" not in json.dumps(c.session.put_calls[-1]["json"]).lower()
