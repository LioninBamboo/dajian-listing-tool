"""Contract tests for the AquaRides Trading content QC runner."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from src.clients.real_ebay_client import RealEbayClient
from src.utils.store_profile import StoreProfile, reset_store_profile_cache


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "trading_content_qc", ROOT / "scripts" / "trading_content_qc.py"
)
qc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = qc
SPEC.loader.exec_module(qc)


def _aqua_profile() -> StoreProfile:
    return StoreProfile(
        store_kind="auto",
        brand_name="AquaRides",
        listing_channel="trading",
        ebay_site_id="100",
        qc_profile="motors",
        scheduler_profile="ops",
        brand_tagline="PERFORMANCE AUTO PARTS",
        description_footer_line1="✦ Ships from US Warehouse ✦",
        quality_footer_marker="ships from",
        template_style="auto_technical",
        force_house_brand=True,
        category_tree_id="100",
    )


def _gp_ops_profile() -> StoreProfile:
    return StoreProfile(
        store_kind="furniture",
        brand_name="GrovePop",
        listing_channel="inventory",
        ebay_site_id="0",
        qc_profile="furniture",
        scheduler_profile="ops",
    )


@pytest.fixture(autouse=True)
def _reset_profile_cache():
    reset_store_profile_cache()
    yield
    reset_store_profile_cache()


def _listing(**overrides):
    data = {
        "sku": "SKU-1",
        "listing_id": "188752675328",
        "title": "Universal Roof Rack Cargo Basket",
        "source_description": "Grounded source features",
        "attributes": {"Material": "Steel"},
        "specs": {"Assembled Length (in.)": "50"},
        "aspects": {"Brand": ["AquaRides"]},
    }
    data.update(overrides)
    return qc.PublishedListing(**data)


def _valid_description():
    return (
        "<div><h1>AQUARIDES</h1><h3>KEY FEATURES</h3>"
        + ("Grounded feature detail. " * 90)
        + "<h3>SPECIFICATIONS</h3><table><tr><td>Steel</td></tr></table>"
        + "<p>Universal fit. Ships from US Warehouse.</p></div>"
    )


def test_decode_live_description_handles_utf8_replacement_and_double_escaping():
    raw = b"&amp;lt;div&amp;gt;A&amp;amp;B \xc3\x97 C&amp;lt;/div&amp;gt;"
    decoded = qc.decode_live_description(raw)
    assert decoded == "<div>A&B × C</div>"


def test_extract_description_from_trading_response_decodes_xml_and_html_layers():
    payload = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
        b"<Ack>Success</Ack><Item><Description>&amp;lt;div&amp;gt;AquaRides&amp;lt;/div&amp;gt;</Description></Item>"
        b"</GetItemResponse>"
    )
    assert qc.extract_trading_description(payload) == "<div>AquaRides</div>"


def test_extract_description_unwraps_cdata():
    payload = b"<GetItemResponse><Description><![CDATA[<div>AQUARIDES</div>]]></Description></GetItemResponse>"
    assert qc.extract_trading_description(payload) == "<div>AQUARIDES</div>"


def test_generate_description_emits_gold_not_furniture_shell(monkeypatch):
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", _aqua_profile)
    desc = qc.generate_description(
        _listing(
            title="Class 3 Trailer Hitch 2 Inch Receiver",
            source_description="Carbon steel Class 3 hitch with 2 inch receiver.",
            attributes={"Material": "Carbon Steel"},
            specs={"Assembled Length (in.)": "46.5"},
            aspects={
                "Type": ["Trailer Hitch"],
                "Material": ["Carbon Steel"],
                "Item Length": ["46.5 in"],
                "Item Width": ["12 in"],
                "Item Height": ["6 in"],
            },
        )
    )
    from src.services.auto_technical_prompt import looks_like_gold_template

    assert looks_like_gold_template(desc)
    assert "overwhelming the room" not in desc.lower()
    assert "factory-style" not in desc.lower()
    assert "purpose-built" not in desc.lower()
    assert "installation on the vehicle" not in desc.lower()


def test_check_description_does_not_flag_gold_chrome_as_thin():
    from src.services.auto_technical_prompt import build_auto_gold_description

    gold = build_auto_gold_description(
        title="Class 3 Trailer Hitch 2 Inch Receiver",
        source_description="Carbon steel hitch.",
        attributes={"Material": "Carbon Steel"},
        specs={},
        aspects={"Type": ["Trailer Hitch"], "Material": ["Carbon Steel"]},
        profile=_aqua_profile(),
    )
    check = qc.check_description(gold)
    assert "thin_description" not in {issue["code"] for issue in check["issues"]}
    assert check["actionable"] is False


def test_check_description_assigns_required_severities_and_does_not_flag_clean_universal_copy():
    assert qc.check_description(_valid_description())["issues"] == []

    broken = "<div>Assembly Required \ufffd 中文</div>"
    issues = qc.check_description(broken)["issues"]
    by_code = {issue["code"]: issue for issue in issues}
    assert by_code["mojibake"]["severity"] == "CRITICAL"
    assert by_code["raw_cjk"]["severity"] == "CRITICAL"
    assert by_code["missing_banner"]["severity"] == "HIGH"
    assert by_code["missing_key_features"]["severity"] == "HIGH"
    assert by_code["thin_description"]["severity"] == "HIGH"
    assert by_code["assembly_copy"]["severity"] == "MEDIUM"


def test_check_description_flags_raw_cjk_in_live_text_after_ascii_safe_drops_it():
    from src.services.motors_trading import ascii_safe

    live = "<div>AQUARIDES KEY FEATURES 钢制 hitch</div>"
    assert "钢制" in live
    assert ascii_safe(live) == "AQUARIDES KEY FEATURES  hitch" or "钢制" not in ascii_safe(live)
    issues = {issue["code"] for issue in qc.check_description(live)["issues"]}
    assert "raw_cjk" in issues


def test_load_published_rows_is_status_and_listing_id_scoped_with_json_aspects():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT, listing_id TEXT, title TEXT, description TEXT, "
        "attributes TEXT, specs TEXT, optimization TEXT, status TEXT)"
    )
    conn.executemany(
        "INSERT INTO collected_products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("published", "1", "T", "D", "{}", "{}", json.dumps({"aspects": {"Brand": ["AquaRides"]}}), "PUBLISHED"),
            ("no-id", "", "T", "D", "{}", "{}", "{}", "PUBLISHED"),
            ("ready", "2", "T", "D", "{}", "{}", "{}", "READY"),
        ],
    )
    rows = qc.load_published_rows(conn)
    assert [(row.sku, row.listing_id, row.aspects) for row in rows] == [
        ("published", "1", {"Brand": ["AquaRides"]})
    ]


def test_run_qc_fixes_only_critical_and_high_and_verifies_live_readback(monkeypatch):
    bad = "<div>\ufffd 中文</div>"
    fixed = _valid_description()

    class FakeGateway:
        def __init__(self):
            self.live = {"1": bad, "2": _valid_description().replace("AQUARIDES", "")}
            self.calls = []

        def get_live_description(self, listing_id):
            self.calls.append(("get", listing_id))
            return self.live[listing_id]

        def revise_description(self, listing_id, description, aspects=None):
            self.calls.append(("revise", listing_id, description))
            self.live[listing_id] = fixed
            return {"ack": "Warning"}

        def get_inventory_item(self, sku):
            self.calls.append(("inventory", sku))
            return None

        def update_inventory_description(self, sku, listing_id, description):
            raise AssertionError("Inventory path should not be used")

    monkeypatch.setattr(qc, "generate_description", lambda listing: fixed)
    gateway = FakeGateway()
    report = qc.run_qc(
        [_listing(listing_id="1"), _listing(sku="SKU-2", listing_id="2")],
        gateway=gateway,
        fix=True,
        sleep_seconds=0.4,
    )

    assert report["fixed_count"] == 2
    assert [call[0] for call in gateway.calls].count("revise") == 2
    assert all(item["post_check"]["actionable"] is False for item in report["items"])


def test_run_qc_leaves_medium_only_listing_untouched():
    medium_only = _valid_description().replace(
        "Universal fit.", "Assembly Required. Universal fit."
    )

    class ForbiddenGateway:
        def get_live_description(self, listing_id):
            return medium_only

        def revise_description(self, listing_id, description, aspects=None):
            raise AssertionError("MEDIUM-only issue must not be fixed")

        def get_inventory_item(self, sku):
            raise AssertionError("MEDIUM-only issue must not inspect Inventory")

        def update_inventory_description(self, sku, listing_id, description):
            raise AssertionError("MEDIUM-only issue must not update Inventory")

    report = qc.run_qc([_listing()], gateway=ForbiddenGateway(), fix=True, sleep_seconds=0.4)
    assert report["fixed_count"] == 0
    assert report["items"][0]["status"] == "checked"
    assert report["items"][0]["issues"][0]["severity"] == "MEDIUM"


def test_inventory_listing_uses_offer_put_and_publish_without_trading_revise():
    bad = "<div>\ufffd 中文</div>"
    fixed = _valid_description()

    class FakeGateway:
        def __init__(self):
            self.calls = []
            self.live = bad

        def get_live_description(self, listing_id):
            self.calls.append(("get-live", listing_id))
            return self.live

        def find_inventory_offer(self, sku, listing_id):
            self.calls.append(("find-offer", sku, listing_id))
            return {"offerId": "offer-1"}

        def update_inventory_description(self, sku, listing_id, description):
            self.calls.append(("inventory-update", sku, listing_id, description))
            self.live = fixed
            return {"channel": "inventory", "offer_id": "offer-1", "ack": "Success"}

    original = qc.generate_description
    qc.generate_description = lambda listing: fixed
    try:
        gateway = FakeGateway()
        report = qc.run_qc(
            [_listing(sku="W2913P449891", listing_id="188683585596")],
            gateway=gateway,
            fix=True,
            sleep_seconds=0.4,
        )
    finally:
        qc.generate_description = original

    assert report["fixed_count"] == 1
    assert [call[0] for call in gateway.calls] == ["get-live", "find-offer", "inventory-update", "get-live"]


def test_socket_set_without_count_is_blocked_at_run_qc():
    class FakeGateway:
        def __init__(self):
            self.writes = []

        def get_live_description(self, listing_id):
            return "<div>broken thin copy</div>"

        def find_inventory_offer(self, sku, listing_id):
            return None

        def revise_description(self, listing_id, description, aspects=None):
            self.writes.append((listing_id, description))
            return {"ack": "Success"}

        def update_inventory_description(self, sku, listing_id, description):
            self.writes.append((sku, listing_id, description))
            return {"channel": "inventory", "ack": "Success"}

    gateway = FakeGateway()
    report = qc.run_qc(
        [
            _listing(
                sku="SYNTHETIC-SOCKET-SET",
                listing_id="188000000004",
                title="Socket Set",
                source_description="",
                aspects={"Type": ["Socket Set"]},
            )
        ],
        gateway=gateway,
        fix=True,
        sleep_seconds=0.4,
    )
    assert report["fixed_count"] == 0
    assert report["items"][0]["status"] == "fix_failed"
    assert "package includes" in report["items"][0]["error"]
    assert gateway.writes == []


def test_c1_revise_description_matches_real_client_signature():
    client = create_autospec(RealEbayClient, instance=True)
    client.revise_fixed_price_item_motors.return_value = {"ack": "Success"}

    class Profile:
        ebay_site_id = "100"

    gateway = qc.EbayContentGateway(client, Profile())
    gateway.revise_description(
        "188752675328",
        "<div>clean</div>",
        aspects={"Brand": ["AquaRides"], "Type": ["Hitch"]},
    )
    client.revise_fixed_price_item_motors.assert_called_once_with(
        "188752675328", description="<div>clean</div>"
    )


def test_c2_inventory_put_preserves_get_category_qty_and_price():
    class Response:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.puts = []

        def get(self, url, **kwargs):
            if url.endswith("/offer"):
                return Response(200, {"offers": [get_offer]})
            return Response(200, get_offer)

        def put(self, url, **kwargs):
            self.puts.append((url, kwargs))
            return Response(204)

    get_offer = {
        "offerId": "offer-1",
        "categoryId": "33653",
        "availableQuantity": 0,
        "pricingSummary": {"price": {"value": "89.99", "currency": "USD"}},
        "listingDescription": "old",
        "listingDuration": "GTC",
        "sku": "W2913P449891",
        "listing": {"listingId": "188683585596"},
        "tax": {"applyTax": False},
        "storeCategoryNames": ["Auto"],
        "regulatory": {"hazmat": False},
        "includeCatalogProductDetails": True,
        "quantityLimitPerBuyer": 4,
        "lotSize": 1,
        "secondaryCategoryId": "123",
        "listingStartQuantity": 0,
        "hideBuyerDetails": True,
        "mysteryExtraField": "must-survive",
    }

    class FakeClient:
        base_url = "https://api.ebay.example"

        def __init__(self):
            self.offer = dict(get_offer)
            self.session = Session()
            self.oauth = type("O", (), {"get_valid_token": staticmethod(lambda: "token")})()

        def get_offers_by_sku(self, sku, marketplace_id=None):
            return [self.offer]

        def get_offer(self, offer_id):
            return dict(self.offer)

        def update_offer_category(self, *args, **kwargs):
            raise AssertionError("C2 path must PUT the GET snapshot, not update_offer_category")

        def publish_offer(self, offer_id):
            return {"listingId": "188683585596"}

    class Profile:
        ebay_marketplace_id = "EBAY_US"

    client = FakeClient()
    gateway = qc.EbayContentGateway(client, Profile())
    # Seed listingDescription so readback matches the ascii_safe PUT.
    from src.services.motors_trading import ascii_safe

    safe = ascii_safe("<div>AQUARIDES KEY FEATURES clean</div>")
    client.offer["listingDescription"] = safe
    result = gateway.update_inventory_description(
        "W2913P449891", "188683585596", "<div>AQUARIDES KEY FEATURES clean</div>"
    )
    assert result["channel"] == "inventory"
    assert client.session.puts
    body = client.session.puts[0][1]["json"]
    assert body["categoryId"] == get_offer["categoryId"]
    assert body["availableQuantity"] == 0
    assert body["pricingSummary"] == get_offer["pricingSummary"]
    assert body["tax"] == get_offer["tax"]
    assert body["storeCategoryNames"] == get_offer["storeCategoryNames"]
    assert body["regulatory"] == get_offer["regulatory"]
    assert body["includeCatalogProductDetails"] is True
    assert body["quantityLimitPerBuyer"] == 4
    assert body["lotSize"] == 1
    assert body["secondaryCategoryId"] == "123"
    assert body["listingStartQuantity"] == 0
    assert body["hideBuyerDetails"] is True
    assert body["mysteryExtraField"] == "must-survive"
    assert "offerId" not in body
    assert "listing" not in body
    assert "status" not in body


def test_trading_gateway_get_item_sends_item_return_description_and_iaf_token():
    class Response:
        content = (
            b'<GetItemResponse><Ack>Warning</Ack><Item><Description>'
            b"&amp;lt;div&amp;gt;AQUARIDES&amp;lt;/div&amp;gt;"
            b"</Description></Item></GetItemResponse>"
        )

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return Response()

    class OAuth:
        def get_valid_token(self):
            return "token"

    class Client:
        oauth = OAuth()
        session = Session()

    class Profile:
        ebay_site_id = "100"

    gateway = qc.EbayContentGateway(Client(), Profile())
    assert gateway.get_live_description("188752675328") == "<div>AQUARIDES</div>"
    request = Client.session.calls[0]
    assert request[1]["headers"]["X-EBAY-API-CALL-NAME"] == "GetItem"
    assert request[1]["headers"]["X-EBAY-API-IAF-TOKEN"] == "token"
    assert b"ItemReturnDescription" in request[1]["data"]


def test_inventory_gateway_matches_item_id_updates_offer_and_publishes():
    class Response:
        def __init__(self, status_code=200, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    class Session:
        def __init__(self, client):
            self.client = client
            self.puts = []

        def get(self, url, **kwargs):
            if str(url).endswith("/offer"):
                return Response(200, {"offers": [self.client.offer]})
            return Response(200, self.client.offer)

        def put(self, url, **kwargs):
            self.puts.append((url, kwargs))
            return Response(204)

    class FakeClient:
        base_url = "https://api.ebay.example"

        def __init__(self):
            self.offer = {
                "offerId": "offer-1",
                "categoryId": "33653",
                "availableQuantity": 0,
                "pricingSummary": {"price": {"value": "89.99", "currency": "USD"}},
                "listingDescription": "old",
                "listing": {"listingId": "188683585596"},
                "mysteryExtraField": "keep-me",
            }
            self.session = Session(self)
            self.calls = []
            self.oauth = type("O", (), {"get_valid_token": staticmethod(lambda: "token")})()

        def get_offers_by_sku(self, sku, marketplace_id=None):
            self.calls.append(("offers", sku, marketplace_id))
            return [self.offer]

        def get_offer(self, offer_id):
            self.calls.append(("get-offer", offer_id))
            return dict(self.offer)

        def update_offer_category(self, *args, **kwargs):
            raise AssertionError("C2 path must PUT the GET snapshot, not update_offer_category")

        def publish_offer(self, offer_id):
            self.calls.append(("publish", offer_id))
            return {"listingId": "188683585596"}

    class Profile:
        ebay_marketplace_id = "EBAY_US"

    from src.services.motors_trading import ascii_safe

    client = FakeClient()
    safe = ascii_safe("<div>clean</div>")
    client.offer["listingDescription"] = safe
    gateway = qc.EbayContentGateway(client, Profile())
    result = gateway.update_inventory_description("W2913P449891", "188683585596", "<div>clean</div>")
    assert result["channel"] == "inventory"
    assert ("publish", "offer-1") in client.calls
    assert client.session.puts
    body = client.session.puts[0][1]["json"]
    assert body["categoryId"] == "33653"
    assert body["availableQuantity"] == 0
    assert body["pricingSummary"]["price"]["value"] == "89.99"
    assert body["mysteryExtraField"] == "keep-me"
    assert body["listingDescription"] == safe


def test_inventory_put_fails_closed_without_session_and_does_not_change_qty():
    class FakeClient:
        def __init__(self):
            self.offer = {
                "offerId": "offer-1",
                "categoryId": "33653",
                "availableQuantity": 0,
                "listingDescription": "old",
                "listing": {"listingId": "188683585596"},
            }

        def get_offers_by_sku(self, sku, marketplace_id=None):
            return [self.offer]

        def get_offer(self, offer_id):
            return dict(self.offer)

        def update_offer_category(self, *args, **kwargs):
            raise AssertionError("no session fallback must not call update_offer_category")

        def publish_offer(self, offer_id):
            raise AssertionError("no session fallback must not publish")

    class Profile:
        ebay_marketplace_id = "EBAY_US"

    gateway = qc.EbayContentGateway(FakeClient(), Profile())
    with pytest.raises(RuntimeError, match="HTTP session"):
        gateway.update_inventory_description("W2913P449891", "188683585596", "<div>clean</div>")


def test_inventory_gateway_fails_closed_when_inventory_item_exists_without_matching_offer():
    class FakeClient:
        def get_offers_by_sku(self, sku, marketplace_id=None):
            return []

        def get_inventory_item(self, sku):
            return {"sku": sku}

    class Profile:
        ebay_marketplace_id = "EBAY_US"

    gateway = qc.EbayContentGateway(FakeClient(), Profile())
    try:
        gateway.find_inventory_offer("W2913P449891", "188683585596")
    except RuntimeError as exc:
        assert "Inventory listing" in str(exc)
    else:
        raise AssertionError("missing Inventory offer must fail closed")


def test_inventory_gateway_does_not_treat_offer_api_error_as_trading_listing():
    class Response:
        status_code = 500

        def json(self):
            return {}

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    class OAuth:
        def get_valid_token(self):
            return "token"

    class Client:
        base_url = "https://api.ebay.example"
        oauth = OAuth()
        session = Session()

    class Profile:
        ebay_marketplace_id = "EBAY_US"

    gateway = qc.EbayContentGateway(Client(), Profile())
    try:
        gateway.find_inventory_offer("SKU-1", "1")
    except RuntimeError as exc:
        assert "offer lookup failed" in str(exc)
    else:
        raise AssertionError("Inventory API errors must fail closed")


def test_render_email_html_excludes_medium_only_rows():
    report = {
        "total_published": 2,
        "total_checked": 2,
        "fixed_count": 0,
        "severity_counts": {"CRITICAL": 1, "HIGH": 1, "MEDIUM": 1},
        "items": [
            {
                "sku": "HIGH-SKU",
                "listing_id": "1",
                "title": "Needs action",
                "issues": [{"code": "missing_banner", "severity": "HIGH", "detail": "missing"}],
                "fixes_applied": [],
                "status": "checked",
            },
            {
                "sku": "MEDIUM-SKU",
                "listing_id": "2",
                "title": "Assembly only",
                "issues": [{"code": "assembly_copy", "severity": "MEDIUM", "detail": "Assembly Required"}],
                "fixes_applied": [],
                "status": "checked",
            },
        ],
        "transport_issues": [],
    }
    body = qc.render_email_html(report)
    assert "需处理 1 条：CRITICAL 0 / HIGH 1" in body
    assert "HIGH-SKU" in body
    assert "MEDIUM-SKU" not in body
    assert "Assembly Required" not in body


def test_unresolved_severity_counts_use_post_fix_state():
    report = {
        "items": [
            {
                "status": "fixed",
                "issues": [{"severity": "HIGH"}],
                "post_check": {"issues": []},
            },
            {
                "status": "verify_failed",
                "issues": [{"severity": "HIGH"}],
                "post_check": {"issues": [{"severity": "CRITICAL"}]},
            },
        ]
    }
    assert qc.unresolved_severity_counts(report) == {"CRITICAL": 1, "HIGH": 0}


@pytest.mark.parametrize(
    ("profile", "label"),
    [
        (StoreProfile(), "main-default"),
        (_gp_ops_profile(), "gp-ops"),
    ],
    ids=["main-default", "gp-ops"],
)
def test_profile_guard_refuses_furniture_stores_without_client_calls(monkeypatch, profile, label):
    del label
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: profile)
    client_built = []

    def _boom():
        client_built.append(True)
        raise AssertionError("furniture profile must not build an eBay client")

    monkeypatch.setattr(qc, "_build_gateway", _boom)
    rc = qc.main([])
    assert rc == 2
    assert client_built == []


def test_profile_guard_message_names_aquarides_trading():
    with pytest.raises(RuntimeError, match="AquaRides Trading"):
        qc._profile_guard()
