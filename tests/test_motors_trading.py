"""Tests for the eBay Motors Trading AddFixedPriceItem XML builders (P0-A)."""

import re

import pytest

from src.services.motors_trading import (
    build_add_fixed_price_item_xml,
    format_compatibility_list,
)

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
