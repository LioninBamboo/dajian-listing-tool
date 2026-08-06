"""P0-A step 4 — Motors fitment pipeline: tree-aware gate + end-to-end dry-run.

Guards the seam where a tree-100 Motors store's structured fitment must survive
all the way into the Trading AddFixedPriceItem XML. Without the ``is_motors_store``
bypass, analyze() gated on the tree-0 whitelist and silently dropped fitment for
tree-100 leaf ids (33653 hitch, 33650 running board, …), so a hitch would publish
with no ItemCompatibilityList. These are offline (no eBay call) — the canary
rehearsal for the live publish.
"""

from src.services.motors_trading import build_add_fixed_price_item_xml
from src.services.vehicle_compatibility import analyze_ebay_motors_compatibility

_HITCH_ASPECTS = {
    "Vehicle Make": ["Ford"],
    "Vehicle Model": ["Ranger"],
    "Vehicle Year": ["2019-2021"],
}
_POLICIES = {
    "fulfillmentPolicyId": "262397301013",
    "returnPolicyId": "262619354013",
    "paymentPolicyId": "262397299013",
}


class TestTreeAwareGate:
    def test_motors_store_gets_fitment_for_tree100_id(self):
        # 33653 is a tree-100 leaf, NOT in the tree-0 EBAY_MOTORS_CATEGORIES set.
        r = analyze_ebay_motors_compatibility(
            "33653", 'Class 3 Trailer Hitch 2" Receiver', "Direct replacement rear hitch",
            _HITCH_ASPECTS, is_motors_store=True,
        )
        assert r.mode == "specific"
        assert len(r.compatible_products) == 3            # 2019, 2020, 2021 Ford Ranger
        first = r.compatible_products[0]["compatibilityProperties"]
        names = {p["name"]: p["value"] for p in first}
        assert names["Make"] == "Ford" and names["Model"] == "Ranger"

    def test_default_keeps_tree0_gate_for_tree100_id(self):
        # Same id, no bypass -> unchanged not_applicable (main-store safety).
        r = analyze_ebay_motors_compatibility("33653", "Trailer Hitch", "x", _HITCH_ASPECTS)
        assert r.mode == "not_applicable"
        assert r.compatible_products == []

    def test_furniture_category_never_gets_fitment(self):
        r = analyze_ebay_motors_compatibility("20487", "Kitchen Pantry Cabinet", "x", {})
        assert r.mode == "not_applicable"

    def test_explicit_tree0_motors_id_still_works_without_flag(self):
        # 174020 IS in the tree-0 set — existing callers must be unaffected.
        r = analyze_ebay_motors_compatibility(
            "174020", "Trailer Hitch", "rear hitch", _HITCH_ASPECTS
        )
        assert r.mode == "specific"

    def test_tool_in_motors_store_yields_no_bogus_fitment(self):
        # A tool has no vehicle data; the parser must not invent fitment.
        r = analyze_ebay_motors_compatibility(
            "33653", "1/2 in Cordless Impact Wrench Kit", "high torque", {},
            is_motors_store=True,
        )
        assert r.mode != "specific"
        assert r.compatible_products == []


class TestEndToEndDryRun:
    """Rehearse batch_publish's Trading branch offline: analyze -> XML."""

    def test_hitch_fitment_reaches_trading_xml(self):
        analysis = analyze_ebay_motors_compatibility(
            "33653", 'Class 3 Trailer Hitch 2" Receiver 5000 lb',
            "Bolt-on direct replacement, no drilling.", _HITCH_ASPECTS,
            is_motors_store=True,
        )
        # This is exactly what publish_single_product passes when channel==trading.
        compatibility = (
            analysis.compatible_products if analysis.mode != "not_applicable" else []
        )
        xml = build_add_fixed_price_item_xml(
            title='Class 3 Trailer Hitch 2" Receiver 5000 lb Towing',
            description="<div>Bolt-on direct replacement.</div>",
            category_id="33653",
            price=139.99,
            quantity=5,
            policies=_POLICIES,
            location="Los Angeles, CA",
            postal_code="90001",
            aspects={"Brand": ["AquaRides"], "Type": ["Receiver Hitch"]},
            image_urls=["https://img/hitch.jpg"],
            compatibility=compatibility,
        )
        assert "<CategoryID>33653</CategoryID>" in xml
        assert "<ItemCompatibilityList>" in xml
        assert xml.count("<Compatibility>") == 3
        assert "<Name>Make</Name><Value>Ford</Value>" in xml
        assert "<Value>Ranger</Value>" in xml
        assert '<StartPrice currencyID="USD">139.99</StartPrice>' in xml

    def test_universal_accessory_publishes_without_compatibility_block(self):
        analysis = analyze_ebay_motors_compatibility(
            "33653", "Universal Aluminum Roof Cargo Basket", "fits most crossbars", {},
            is_motors_store=True,
        )
        compatibility = (
            analysis.compatible_products if analysis.mode != "not_applicable" else []
        )
        xml = build_add_fixed_price_item_xml(
            title="Universal Aluminum Roof Cargo Basket",
            description="<div>Universal fit.</div>",
            category_id="33653",
            price=89.99,
            quantity=5,
            policies=_POLICIES,
            location="Los Angeles, CA",
            postal_code="90001",
            aspects={"Brand": ["AquaRides"]},
            image_urls=["https://img/basket.jpg"],
            compatibility=compatibility,
        )
        assert "<ItemCompatibilityList>" not in xml     # no fabricated fitment
        assert "<CategoryID>33653</CategoryID>" in xml
