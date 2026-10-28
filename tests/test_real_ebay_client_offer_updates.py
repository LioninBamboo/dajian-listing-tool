import pytest

from src.clients.real_ebay_client import RealEbayClient
from src.utils.ebay_quantity import SupplierOutOfStockError


class _DummyOauth:
    api_base = "https://api.ebay.example"

    def get_valid_token(self):
        return "token"


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Session:
    def __init__(self, offer_payload, put_response):
        self.offer_payload = offer_payload
        self.put_response = put_response
        self.put_calls = []

    def get(self, url, headers=None, timeout=None):
        return _Response(200, payload=self.offer_payload)

    def put(self, url, headers=None, json=None, timeout=None):
        self.put_calls.append(
            {
                "url": url,
                "headers": headers or {},
                "json": json or {},
            }
        )
        return self.put_response


def _make_offer(*, category_id="38204", listing_description="<div>old desc</div>"):
    return {
        "availableQuantity": 2,
        "categoryId": category_id,
        "listingDescription": listing_description,
        "listingDuration": "GTC",
        "listingPolicies": {
            "fulfillmentPolicyId": "1",
            "returnPolicyId": "2",
            "paymentPolicyId": "3",
        },
        "merchantLocationKey": "DAJIAN_LA_WAREHOUSE",
        "pricingSummary": {
            "price": {
                "value": "99.99",
                "currency": "USD",
            }
        },
    }


def _make_client(*, initial_offer, put_response, verified_offer):
    client = object.__new__(RealEbayClient)
    client.base_url = "https://api.ebay.example"
    client.oauth = _DummyOauth()
    client.session = _Session(initial_offer, put_response)
    client._complete_listing_policies = lambda policies=None: policies or {}
    client.get_offer = lambda offer_id: verified_offer
    return client


def _long_description(label: str) -> str:
    return f"<div>{label} {'detail ' * 12}</div>"


def test_update_offer_category_treats_verified_state_as_success():
    target_description = _long_description("new desc")
    client = _make_client(
        initial_offer=_make_offer(),
        put_response=_Response(
            400,
            text='{"errors":[{"errorId":25002,"message":"UPC field is missing"}]}',
        ),
        verified_offer=_make_offer(
            category_id="183124",
            listing_description=target_description,
        ),
    )

    ok = client.update_offer_category(
        "offer-1",
        "183124",
        listing_description=target_description,
    )

    assert ok is True
    assert client.session.put_calls[0]["json"]["categoryId"] == "183124"


def test_update_offer_category_keeps_failure_when_follow_up_state_is_wrong():
    client = _make_client(
        initial_offer=_make_offer(),
        put_response=_Response(
            400,
            text='{"errors":[{"errorId":25002,"message":"UPC field is missing"}]}',
        ),
        verified_offer=_make_offer(
            category_id="38204",
            listing_description=_long_description("old desc"),
        ),
    )

    ok = client.update_offer_category(
        "offer-1",
        "183124",
        listing_description=_long_description("new desc"),
    )

    assert ok is False


class _PostSession:
    def __init__(self):
        self.post_calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls.append(json or {})
        return _Response(201, payload={"offerId": "offer-qty"})


def test_create_offer_sends_resolved_available_quantity(monkeypatch):
    monkeypatch.setattr(
        "src.utils.ebay_quantity.resolve_publish_quantity",
        lambda sku, **kwargs: 4,
    )
    client = object.__new__(RealEbayClient)
    client.base_url = "https://api.ebay.example"
    client.oauth = _DummyOauth()
    client.session = _PostSession()
    client.marketplace_id = "EBAY_US"
    client._complete_listing_policies = lambda policies=None: {}

    result = client.create_offer("SKU-1", 99.0, category_id="38204")

    assert result["offerId"] == "offer-qty"
    assert client.session.post_calls[0]["availableQuantity"] == 4


def test_create_offer_keeps_resolved_quantity_of_one(monkeypatch):
    monkeypatch.setattr("src.utils.ebay_quantity.default_ebay_listing_quantity", lambda: 2)
    monkeypatch.setattr(
        "src.utils.ebay_quantity.resolve_publish_quantity",
        lambda sku, **kwargs: 1,
    )
    client = object.__new__(RealEbayClient)
    client.base_url = "https://api.ebay.example"
    client.oauth = _DummyOauth()
    client.session = _PostSession()
    client.marketplace_id = "EBAY_US"
    client._complete_listing_policies = lambda policies=None: {}

    result = client.create_offer("SKU-1", 99.0, category_id="38204")

    assert result["offerId"] == "offer-qty"
    assert client.session.post_calls[0]["availableQuantity"] == 1


def test_update_offer_category_defaults_missing_quantity_to_listing_default(monkeypatch):
    monkeypatch.setattr("src.utils.ebay_quantity.default_ebay_listing_quantity", lambda: 2)
    monkeypatch.setattr(
        "src.utils.ebay_quantity.resolve_publish_quantity",
        lambda sku, **kwargs: 2,
    )
    initial = _make_offer()
    initial.pop("availableQuantity", None)
    initial["sku"] = "SKU-1"
    client = _make_client(
        initial_offer=initial,
        put_response=_Response(200, payload={}),
        verified_offer=initial,
    )

    ok = client.update_offer_category(
        "offer-1",
        "183124",
        listing_description=_long_description("new desc"),
    )

    assert ok is True
    assert client.session.put_calls[0]["json"]["availableQuantity"] == 2


def test_create_offer_propagates_zero_supplier_stock(monkeypatch):
    monkeypatch.setattr(
        "src.utils.ebay_quantity.resolve_publish_quantity",
        lambda sku, **kwargs: (_ for _ in ()).throw(SupplierOutOfStockError(f"{sku} has zero supplier inventory")),
    )
    client = object.__new__(RealEbayClient)
    client.base_url = "https://api.ebay.example"
    client.oauth = _DummyOauth()
    client.session = _PostSession()
    client.marketplace_id = "EBAY_US"
    client._complete_listing_policies = lambda policies=None: {}

    with pytest.raises(SupplierOutOfStockError):
        client.create_offer("SKU-OOS", 99.0, category_id="38204")
    assert client.session.post_calls == []


def test_update_offer_category_keeps_live_qty_when_supplier_oos(monkeypatch):
    monkeypatch.setattr(
        "src.utils.ebay_quantity.resolve_publish_quantity",
        lambda sku, **kwargs: (_ for _ in ()).throw(SupplierOutOfStockError(f"{sku} has zero supplier inventory")),
    )
    initial = _make_offer()
    initial["sku"] = "SKU-OOS"
    initial["availableQuantity"] = 0
    verified = dict(initial)
    verified["listingDescription"] = _long_description("new desc")
    client = _make_client(
        initial_offer=initial,
        put_response=_Response(200, payload={}),
        verified_offer=verified,
    )

    ok = client.update_offer_category(
        "offer-1",
        "183124",
        listing_description=_long_description("new desc"),
    )
    assert ok is True
    assert client.session.put_calls[0]["json"]["availableQuantity"] == 0


def test_update_offer_category_oos_qty_zero_skips_selling_limit_restock(monkeypatch):
    """Selling-limit fallback must not raise live qty 0 → 1 on OOS content updates."""
    monkeypatch.setattr(
        "src.utils.ebay_quantity.resolve_publish_quantity",
        lambda sku, **kwargs: (_ for _ in ()).throw(SupplierOutOfStockError(f"{sku} has zero supplier inventory")),
    )
    initial = _make_offer()
    initial["sku"] = "SKU-OOS"
    initial["availableQuantity"] = 0
    client = _make_client(
        initial_offer=initial,
        put_response=_Response(
            400,
            text='{"errors":[{"message":"You have exceeded the amount you can list"}]}',
        ),
        # Keep verified category mismatched so success cannot come from GET fallback.
        verified_offer=initial,
    )

    ok = client.update_offer_category(
        "offer-1",
        "183124",
        listing_description=_long_description("new desc"),
    )

    assert ok is False
    assert len(client.session.put_calls) == 1
    assert client.session.put_calls[0]["json"]["availableQuantity"] == 0


def test_update_offer_category_keeps_resolved_quantity_of_one(monkeypatch):
    monkeypatch.setattr("src.utils.ebay_quantity.default_ebay_listing_quantity", lambda: 2)
    monkeypatch.setattr(
        "src.utils.ebay_quantity.resolve_publish_quantity",
        lambda sku, **kwargs: 1,
    )
    initial = _make_offer()
    initial["sku"] = "SKU-1"
    initial["availableQuantity"] = 2
    client = _make_client(
        initial_offer=initial,
        put_response=_Response(200, payload={}),
        verified_offer=initial,
    )

    ok = client.update_offer_category(
        "offer-1",
        "183124",
        listing_description=_long_description("new desc"),
    )

    assert ok is True
    assert client.session.put_calls[0]["json"]["availableQuantity"] == 1
