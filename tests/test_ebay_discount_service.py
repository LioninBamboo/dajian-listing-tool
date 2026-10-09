from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_promotion_aware_offer_update_sends_inventory_content_language(monkeypatch):
    sys.modules.setdefault(
        "requests",
        types.SimpleNamespace(get=None, put=None, post=None, delete=None, request=None),
    )
    sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))

    import src.services.ebay_discount_service as discount_mod

    class _OAuth:
        def get_valid_token(self):
            return "token"

    service = discount_mod.EbayDiscountService.__new__(discount_mod.EbayDiscountService)
    service.oauth = _OAuth()
    service.base = "https://api.ebay.com"

    put_headers = []

    def fake_get(url, headers=None, timeout=None, verify=None):
        assert "Content-Language" not in headers
        assert url.endswith("/sell/inventory/v1/offer?sku=SKU-1&marketplace_id=EBAY_US")
        return _Response(
            200,
            {
                "offers": [
                    {
                        "offerId": "OFFER-1",
                        "listing": {"listingId": "LISTING-1"},
                        "pricingSummary": {"price": {"value": "10.00"}},
                    }
                ]
            },
        )

    def fake_put(url, headers=None, json=None, timeout=None, verify=None):
        put_headers.append(dict(headers or {}))
        assert url.endswith("/sell/inventory/v1/offer/OFFER-1")
        assert json["pricingSummary"]["price"]["value"] == "12.34"
        return _Response(204)

    monkeypatch.setattr(discount_mod.requests, "get", fake_get)
    monkeypatch.setattr(discount_mod.requests, "put", fake_put)

    result = service.update_price_through_promotion("SKU-1", 12.34)

    assert result["price_updated"] is True
    assert put_headers
    assert put_headers[0]["Content-Language"] == "en-US"
