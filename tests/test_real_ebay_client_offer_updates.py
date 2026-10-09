from src.clients.real_ebay_client import RealEbayClient


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
