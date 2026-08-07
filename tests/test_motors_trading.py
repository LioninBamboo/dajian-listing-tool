"""Tests for the eBay Motors Trading AddFixedPriceItem XML builders (P0-A)."""

import re

import pytest

from src.services.motors_trading import (
    build_add_fixed_price_item_xml,
    build_revise_fixed_price_item_xml,
    format_compatibility_list,
)


class TestReviseBuilder:
    def test_partial_update_only_sends_given_fields(self):
        xml = build_revise_fixed_price_item_xml(item_id="188752675328", description="<b>Clean</b>")
        assert "<ItemID>188752675328</ItemID>" in xml
        assert "<![CDATA[<b>Clean</b>]]>" in xml
        assert "<Title>" not in xml                      # omitted field left untouched live
        assert "<ItemSpecifics>" not in xml              # fitment/aspects preserved
        assert xml.startswith("<?xml")

    def test_title_clipped_to_80(self):
        xml = build_revise_fixed_price_item_xml(item_id="1", title="x" * 120)
        assert "<Title>" + "x" * 80 + "</Title>" in xml

    def test_requires_item_id(self):
        with pytest.raises(ValueError):
            build_revise_fixed_price_item_xml(item_id="", description="d")

_POLICIES = {
    "fulfillmentPolicyId": "262397301013",
    "returnPolicyId": "262619354013",
    "paymentPolicyId": "262397299013",
}

_FITMENT = [
    {"compatibilityProperties": [
        {"name": "Year", "value": "1998"},
        {"name": "Make", "value": "Ford"},
        {"name": "Model", "value": "Ranger"},
    ]},
    {"compatibilityProperties": [
        {"name": "Year", "value": "1999"},
        {"name": "Make", "value": "Ford"},
        {"name": "Model", "value": "Ranger"},
    ]},
]


class TestCompatibility:
    def test_renders_year_make_model(self):
        xml = format_compatibility_list(_FITMENT)
        assert xml.startswith("<ItemCompatibilityList>")
        assert xml.count("<Compatibility>") == 2
        assert "<Name>Make</Name><Value>Ford</Value>" in xml
        assert "<Value>Ranger</Value>" in xml

    def test_empty_for_universal_fit(self):
        assert format_compatibility_list([]) == ""
        assert format_compatibility_list(None) == ""

    def test_skips_incomplete_props(self):
        xml = format_compatibility_list([{"compatibilityProperties": [{"name": "Year", "value": ""}]}])
        assert xml == ""

    def test_escapes_values(self):
        xml = format_compatibility_list([{"compatibilityProperties": [
            {"name": "Model", "value": "A&B <special>"}]}])
        assert "&amp;" in xml and "&lt;" in xml


class TestAddItemXml:
    def _build(self, **over):
        kw = dict(
            title="Class 3 Trailer Hitch 2 Inch Receiver",
            description="<p>Direct replacement.</p>",
            category_id="33653",
            price=129.99,
            quantity=5,
            policies=_POLICIES,
            location="Los Angeles, CA",
            postal_code="90001",
            aspects={"Brand": ["AquaRides"], "Type": ["Receiver Hitch"]},
            image_urls=["https://img/a.jpg"],
            compatibility=_FITMENT,
        )
        kw.update(over)
        return build_add_fixed_price_item_xml(**kw)

    def test_core_fields(self):
        xml = self._build()
        assert "<AddFixedPriceItemRequest" in xml
        assert "<CategoryID>33653</CategoryID>" in xml
        assert '<StartPrice currencyID="USD">129.99</StartPrice>' in xml
        assert "<ListingType>FixedPriceItem</ListingType>" in xml
        assert "<ConditionID>1000</ConditionID>" in xml
        assert "<PostalCode>90001</PostalCode>" in xml

    def test_policies_map_to_seller_profiles(self):
        xml = self._build()
        assert "<ShippingProfileID>262397301013</ShippingProfileID>" in xml
        assert "<ReturnProfileID>262619354013</ReturnProfileID>" in xml   # seller-paid, Motors P&A
        assert "<PaymentProfileID>262397299013</PaymentProfileID>" in xml

    def test_includes_fitment_and_aspects(self):
        xml = self._build()
        assert "<ItemCompatibilityList>" in xml
        assert "<ItemSpecifics>" in xml
        assert "<Value>Receiver Hitch</Value>" in xml

    def test_description_is_cdata(self):
        xml = self._build(description="<p>x & y</p>")
        assert "<![CDATA[<p>x & y</p>]]>" in xml  # not double-escaped

    def test_title_truncated_to_80(self):
        xml = self._build(title="X" * 200)
        m = re.search(r"<Title>(.*?)</Title>", xml)
        assert len(m.group(1)) == 80

    def test_universal_fit_has_no_compatibility_block(self):
        xml = self._build(compatibility=[])
        assert "<ItemCompatibilityList>" not in xml
        # still a valid listing
        assert "<CategoryID>33653</CategoryID>" in xml

    def test_requires_category_and_title(self):
        with pytest.raises(ValueError):
            self._build(category_id="")
        with pytest.raises(ValueError):
            self._build(title="")


# --- production wrapper: real_ebay_client.add_fixed_price_item_motors ----------

import dataclasses  # noqa: E402

from src.clients.real_ebay_client import RealEbayClient  # noqa: E402
from src.utils.store_profile import StoreProfile  # noqa: E402


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


class _CaptureSession:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "data": data})
        return _Resp(self._text)


class _Oauth:
    def get_valid_token(self):
        return "iaf-token"


def _auto_profile():
    return dataclasses.replace(
        StoreProfile(),
        store_kind="auto",
        listing_channel="trading",
        ebay_site_id="100",
        warehouse_location="Los Angeles, CA",
        warehouse_postal="90001",
        fallback_fulfillment_policy_id="262397301013",
        fallback_return_policy_id="262397300013",   # buyer-paid (tools / non-Motors)
        motors_return_policy_id="262619354013",      # seller-paid (Motors P&A mandate)
        fallback_payment_policy_id="262397299013",
    )


class TestReturnPolicySplit:
    def test_motors_publish_uses_seller_paid_return(self):
        # add_fixed_price_item_motors must source motors_listing_policies(), so the
        # seller-paid return id — not the buyer-paid fallback — lands in the XML.
        p = _auto_profile()
        assert p.motors_listing_policies()["returnPolicyId"] == "262619354013"
        assert p.fallback_listing_policies()["returnPolicyId"] == "262397300013"


class TestClientWrapper:
    def _client(self, monkeypatch, resp_text):
        monkeypatch.setattr(
            "src.utils.store_profile.get_store_profile", lambda: _auto_profile()
        )
        c = object.__new__(RealEbayClient)
        c.oauth = _Oauth()
        c.session = _CaptureSession(resp_text)
        return c

    def test_publishes_and_returns_item_id(self, monkeypatch):
        ok = "<AddFixedPriceItemResponse><Ack>Success</Ack><ItemID>188732319492</ItemID></AddFixedPriceItemResponse>"
        c = self._client(monkeypatch, ok)
        out = c.add_fixed_price_item_motors(
            {"title": "Class 3 Trailer Hitch", "description": "<p>x</p>",
             "aspects": {"Brand": ["AquaRides"]}, "images": ["https://img/a.jpg"],
             "compatibility": _FITMENT},
            category_id="33653", price=129.99, quantity=5,
        )
        assert out["itemId"] == "188732319492"
        assert out["status"] == "published"
        call = c.session.calls[0]
        assert call["url"].endswith("/ws/api.dll")
        assert call["headers"]["X-EBAY-API-SITEID"] == "100"          # eBay Motors
        assert call["headers"]["X-EBAY-API-CALL-NAME"] == "AddFixedPriceItem"
        xml = call["data"].decode("utf-8")
        assert "<CategoryID>33653</CategoryID>" in xml
        assert "<ItemCompatibilityList>" in xml                       # fitment carried
        assert "<ReturnProfileID>262619354013</ReturnProfileID>" in xml  # seller-paid (Motors P&A)

    def test_warning_ack_still_succeeds(self, monkeypatch):
        warn = "<r><Ack>Warning</Ack><ItemID>999</ItemID></r>"
        c = self._client(monkeypatch, warn)
        assert c.add_fixed_price_item_motors(
            {"title": "T", "description": "d"}, category_id="33653", price=9.99
        )["itemId"] == "999"

    def test_failure_raises_with_message(self, monkeypatch):
        fail = ("<r><Ack>Failure</Ack><Errors><LongMessage>"
                "non-compliant domestic return policy</LongMessage></Errors></r>")
        c = self._client(monkeypatch, fail)
        with pytest.raises(Exception) as exc:
            c.add_fixed_price_item_motors(
                {"title": "T", "description": "d"}, category_id="33653", price=9.99
            )
        assert "return policy" in str(exc.value)
