"""Inventory offer listing ids and GIGA ``Main Color`` as a Color source.

Fake offer dicts and in-memory rows only. No eBay network.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services import sku_field_edit as sfe
from src.utils import source_checked_draft as scd
from tests.test_sku_field_edit import ROWS, FakeEbay, inventory_item, make_ctx, run
from tests.test_sku_field_edit_source_write import draft_row, run_edit

US = "366778621648"
OTHER = "366778661280"
PREFIX = "36677862164"
SKU = "W1097P462002"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    monkeypatch.setenv("AUDIT_SEMANTIC_FACT_SHEET", "0")
    monkeypatch.delenv("QWEN_API_KEY", raising=False)

    def fake_fact_sheet(*_args, **_kwargs):
        return {"status": "pass", "violations": []}

    monkeypatch.setattr(scd, "check_fact_sheet_violations", fake_fact_sheet)


def _fetch(offers, listing_id=US, *, store="main", sku=SKU, inventory=None):
    class Ebay:
        base_url = "https://api.ebay.com"

        def get_inventory_item(self, _sku):
            if inventory is not None:
                return inventory
            return {
                "product": {"title": "t", "description": "d", "aspects": {}},
                "availability": {"shipToLocationAvailability": {"quantity": 3}},
            }

        def get_offers_by_sku(self, _sku):
            return offers

    plan = sfe.SkuPlan(sku=sku, listing_id=listing_id)
    ctx = sfe.SkuFieldEditContext(
        db_path=Path("unused.db"),
        store=store,
        profile=None,
        ebay_client=Ebay(),
    )
    return sfe.fetch_live(plan, ctx, SimpleNamespace())


def _assert_allowed(plan, offer_id=None):
    assert plan.status != "refused", plan.refuse_reason
    assert plan.refuse_reason == ""
    assert plan.live.get("offer")
    if offer_id is not None:
        assert plan.live["offer"]["offerId"] == offer_id


def _assert_refused(plan, reason):
    assert plan.status == "refused"
    assert plan.refuse_reason == reason
    assert plan.live == {}


def _row(attributes=None, specs=None, description=""):
    return {
        "title": "cabinet",
        "description": description,
        "attributes": {} if attributes is None else attributes,
        "specs": specs or {},
    }


def _color(attributes=None, value="Beige", specs=None, description=""):
    return scd.validate_aspect_value("Color", value, _row(attributes, specs, description))


# --- offer listing id -------------------------------------------------------


def test_offer_listing_id_nested_wins_over_different_top_level():
    """Nested listing.listingId wins even when top-level listingId differs."""
    offer = {
        "listing": {"listingId": US, "listingStatus": "ACTIVE"},
        "listingId": "111",
    }
    assert sfe._offer_listing_id(offer) == US
    assert sfe._offer_listing_id(offer) != "111"


def test_nested_listing_id_matching_db_allows_push():
    offer = {
        "offerId": "us",
        "marketplaceId": "EBAY_US",
        "listing": {"listingId": US, "listingStatus": "ACTIVE"},
    }
    plan = _fetch([offer])
    _assert_allowed(plan, "us")


def test_top_level_listing_id_only_still_matches():
    offer = {"offerId": "legacy", "listingId": US, "status": "PUBLISHED"}
    assert sfe._offer_listing_id(offer) == US
    plan = _fetch([offer])
    _assert_allowed(plan, "legacy")


def test_nested_and_top_level_equal_matches():
    offer = {
        "offerId": "both",
        "listing": {"listingId": US, "listingStatus": "ACTIVE"},
        "listingId": US,
    }
    plan = _fetch([offer])
    _assert_allowed(plan, "both")


def test_nested_match_ignores_different_top_level():
    offer = {
        "offerId": "nested-wins",
        "listing": {"listingId": US, "listingStatus": "ACTIVE"},
        "listingId": OTHER,
    }
    plan = _fetch([offer])
    _assert_allowed(plan, "nested-wins")


def test_nested_id_different_from_db_refuses_even_if_top_level_matches():
    offer = {
        "offerId": "nested-wrong",
        "listing": {"listingId": OTHER, "listingStatus": "ACTIVE"},
        "listingId": US,
    }
    plan = _fetch([offer])
    _assert_refused(plan, "listing_id_mismatch")


@pytest.mark.parametrize(
    "offer",
    [
        {"offerId": "bare", "status": "UNPUBLISHED"},
        {"offerId": "listing-none", "listing": None},
        {"offerId": "listing-empty", "listing": {}},
        {"offerId": "listing-blank", "listing": {"listingId": "", "listingStatus": "ACTIVE"}},
        {"offerId": "listing-space", "listing": {"listingId": "   "}},
        {"offerId": "listing-missing-id", "listing": {"listingStatus": "ACTIVE"}},
        {"offerId": "top-blank", "listingId": ""},
        {"offerId": "top-space", "listingId": "  \n"},
        {"offerId": "bool", "listingId": True},
        {"offerId": "list-id", "listing": {"listingId": [US]}},
    ],
)
def test_absent_listing_id_refuses_offer_not_published(offer):
    plan = _fetch([offer])
    _assert_refused(plan, "offer_not_published")


def test_empty_offer_list_and_none_payload_are_unpublished():
    _assert_refused(_fetch([]), "offer_not_published")
    _assert_refused(_fetch(None), "offer_not_published")


def test_blank_nested_id_falls_through_to_matching_top_level():
    offer = {
        "offerId": "fallthrough",
        "listing": {"listingId": "", "listingStatus": "ACTIVE"},
        "listingId": f"  {US}  ",
    }
    assert sfe._offer_listing_id(offer) == US
    _assert_allowed(_fetch([offer]), "fallthrough")


def test_non_mapping_listing_falls_through_to_top_level():
    offer = {"offerId": "string-listing", "listing": "not-a-dict", "listingId": US}
    assert sfe._offer_listing_id(offer) == US
    _assert_allowed(_fetch([offer]), "string-listing")


def test_int_listing_id_matches_db_string_and_whitespace():
    offer = {
        "offerId": "int-id",
        "listing": {"listingId": int(US), "listingStatus": "ACTIVE"},
        "listingId": "nope",
    }
    assert sfe._offer_listing_id(offer) == US
    _assert_allowed(_fetch([offer], listing_id=f"  {US}\n"), "int-id")
    top = {"offerId": "int-top", "listingId": int(US)}
    _assert_allowed(_fetch([top], listing_id=US), "int-top")


def test_db_int_matches_offer_string():
    plan = sfe.SkuPlan(sku=SKU, listing_id=int(US))
    ctx_offers = [{"offerId": "str-id", "listingId": US}]

    class Ebay:
        base_url = "https://api.ebay.com"

        def get_inventory_item(self, _sku):
            return {"availability": {"shipToLocationAvailability": {"quantity": 1}}}

        def get_offers_by_sku(self, _sku):
            return ctx_offers

    ctx = sfe.SkuFieldEditContext(
        db_path=Path("unused.db"), store="main", profile=None, ebay_client=Ebay()
    )
    _assert_allowed(sfe.fetch_live(plan, ctx, SimpleNamespace()), "str-id")


def test_prefix_and_internal_space_are_not_matches():
    short = {"offerId": "short", "listing": {"listingId": PREFIX}}
    _assert_refused(_fetch([short], listing_id=US), "listing_id_mismatch")
    long = {"offerId": "long", "listingId": US}
    _assert_refused(_fetch([long], listing_id=PREFIX), "listing_id_mismatch")
    spaced = {"offerId": "inner-space", "listingId": US[:6] + " " + US[6:]}
    _assert_refused(_fetch([spaced], listing_id=US), "listing_id_mismatch")


def test_leading_zeros_are_not_coerced():
    padded = {"offerId": "zeros", "listingId": "0" + US}
    _assert_refused(_fetch([padded], listing_id=US), "listing_id_mismatch")


def test_multiple_marketplaces_pick_ebay_us_offer():
    offers = [
        {
            "offerId": "gb",
            "marketplaceId": "EBAY_GB",
            "listing": {"listingId": OTHER, "listingStatus": "ACTIVE"},
        },
        {
            "offerId": "us",
            "marketplaceId": "EBAY_US",
            "listing": {"listingId": US, "listingStatus": "ACTIVE"},
        },
    ]
    _assert_allowed(_fetch(offers), "us")


def test_ebay_us_mismatch_does_not_fall_back_to_another_marketplace():
    offers = [
        {
            "offerId": "gb",
            "marketplaceId": "EBAY_GB",
            "listing": {"listingId": US, "listingStatus": "ACTIVE"},
        },
        {
            "offerId": "us",
            "marketplaceId": "EBAY_US",
            "listing": {"listingId": OTHER, "listingStatus": "ACTIVE"},
        },
    ]
    _assert_refused(_fetch(offers), "listing_id_mismatch")


def test_unpublished_ebay_us_does_not_fall_back_to_other_marketplace():
    offers = [
        {
            "offerId": "gb",
            "marketplaceId": "EBAY_GB",
            "listing": {"listingId": US, "listingStatus": "ACTIVE"},
        },
        {"offerId": "us", "marketplaceId": "EBAY_US", "listing": {}},
    ]
    _assert_refused(_fetch(offers), "offer_not_published")


def test_two_ebay_us_offers_with_listing_ids_refuse_multiple_offers():
    offers = [
        {
            "offerId": "us-a",
            "marketplaceId": "EBAY_US",
            "listing": {"listingId": US, "listingStatus": "ACTIVE"},
        },
        {
            "offerId": "us-b",
            "marketplaceId": "EBAY_US",
            "listing": {"listingId": US, "listingStatus": "ACTIVE"},
        },
    ]
    _assert_refused(_fetch(offers), "multiple_offers")
    different = deepcopy(offers)
    different[1]["listing"]["listingId"] = OTHER
    _assert_refused(_fetch(different), "multiple_offers")


def test_one_published_ebay_us_beside_unpublished_ebay_us_is_used():
    offers = [
        {"offerId": "us-draft", "marketplaceId": "EBAY_US", "status": "UNPUBLISHED"},
        {
            "offerId": "us-live",
            "marketplaceId": " ebay_us ",
            "listing": {"listingId": f" {US} ", "listingStatus": "ACTIVE"},
        },
    ]
    _assert_allowed(_fetch(offers), "us-live")


def test_non_dict_offers_are_skipped():
    good = {"offerId": "us", "marketplaceId": "EBAY_US", "listing": {"listingId": US}}
    _assert_allowed(_fetch([None, "bad", 5, good]), "us")


def test_untagged_offers_still_match_by_id():
    offers = [
        {"offerId": "other", "listingId": OTHER},
        {"offerId": "hit", "listingId": US},
    ]
    _assert_allowed(_fetch(offers), "hit")
    _assert_refused(
        _fetch([{"offerId": "a", "listingId": "1"}, {"offerId": "b", "listingId": "2"}]),
        "listing_id_mismatch",
    )


def test_two_untagged_offers_with_the_same_id_refuse_multiple_offers():
    offers = [
        {"offerId": "a", "listingId": US},
        {"offerId": "b", "listing": {"listingId": US}},
    ]
    _assert_refused(_fetch(offers), "multiple_offers")


def test_float_listing_id_is_not_stringified_into_a_match():
    offer = {"offerId": "float", "listingId": float(US)}
    assert sfe._offer_listing_id(offer) is None
    _assert_refused(_fetch([offer]), "offer_not_published")


def test_trading_fetch_live_does_not_read_inventory_offers():
    calls = {"offers": 0}

    class Ebay:
        base_url = "https://api.ebay.com"

        def get_offers_by_sku(self, _sku):
            calls["offers"] += 1
            raise AssertionError("trading listings must not read getOffers")

        def get_item_description_motors(self, item_id):
            assert item_id == "366333333333"
            return "desc"

        def get_item_compatibility_motors(self, _item_id):
            return [{"keep": True}]

    class Trading:
        def get_item(self, item_id, site_id=None):
            assert str(site_id) == "100"
            assert item_id == "366333333333"
            return (
                "<GetItemResponse><Item>"
                "<Title>Aqua Title</Title>"
                "<Description>desc</Description>"
                f"<ItemID>{item_id}</ItemID>"
                "</Item></GetItemResponse>"
            )

    plan = sfe.SkuPlan(sku="W465P341531", listing_id="366333333333")
    ctx = sfe.SkuFieldEditContext(
        db_path=Path("unused.db"),
        store="aqua",
        profile=None,
        ebay_client=Ebay(),
        trading_client=Trading(),
    )
    out = sfe.fetch_live(plan, ctx, SimpleNamespace())
    assert out.status != "refused", out.refuse_reason
    assert calls["offers"] == 0
    assert out.live["title"] == "Aqua Title"
    assert out.live["description"] == "desc"
    assert out.live["compatibility"] == [{"keep": True}]


def test_live_readback_matches_nested_listing_id_not_first_offer():
    inventory = {
        "product": {"title": "t", "description": "body", "aspects": {}},
        "availability": {"shipToLocationAvailability": {"quantity": 4}},
    }
    us = {
        "offerId": "us",
        "marketplaceId": "EBAY_US",
        "listing": {"listingId": int(US), "listingStatus": "ACTIVE"},
        "listingId": "222",
        "pricingSummary": {"price": {"value": "10.00"}},
        "availableQuantity": 4,
        "listingDescription": "body",
        "categoryId": "1",
    }
    gb = {
        "offerId": "gb",
        "marketplaceId": "EBAY_GB",
        "listingId": "111",
        "pricingSummary": {"price": {"value": "99.00"}},
        "availableQuantity": 1,
        "listingDescription": "other",
        "categoryId": "9",
    }

    class Ebay:
        def get_inventory_item(self, _sku):
            return inventory

        def get_offers_by_sku(self, _sku):
            return [gb, us]

    plan = sfe.SkuPlan(
        sku=SKU,
        listing_id=f" {US} ",
        live={"inventory": inventory, "offer": us},
    )
    ctx = sfe.SkuFieldEditContext(
        db_path=Path("unused.db"), store="main", profile=None, ebay_client=Ebay()
    )
    ok, mismatches = sfe.live_readback_ok(plan, ctx)
    assert ok, mismatches


def test_live_readback_still_matches_top_level_listing_id():
    inventory = {
        "product": {"title": "t", "description": "body", "aspects": {}},
        "availability": {"shipToLocationAvailability": {"quantity": 2}},
    }
    decoy = {
        "offerId": "decoy",
        "listingId": "111",
        "pricingSummary": {"price": {"value": "1"}},
        "availableQuantity": 9,
    }
    hit = {
        "offerId": "hit",
        "listingId": US,
        "pricingSummary": {"price": {"value": "10"}},
        "availableQuantity": 2,
        "listingDescription": "body",
    }

    class Ebay:
        def get_inventory_item(self, _sku):
            return inventory

        def get_offers_by_sku(self, _sku):
            return [decoy, hit]

    plan = sfe.SkuPlan(sku=SKU, listing_id=US, live={"inventory": inventory, "offer": hit})
    ctx = sfe.SkuFieldEditContext(
        db_path=Path("unused.db"), store="main", profile=None, ebay_client=Ebay()
    )
    ok, mismatches = sfe.live_readback_ok(plan, ctx)
    assert ok, mismatches


def test_push_live_nested_ebay_us_listing_id_allows_write(tmp_path):
    row = deepcopy(ROWS[2])
    item = inventory_item(row)
    us = item["offer"]
    us.pop("listingId", None)
    us["marketplaceId"] = "EBAY_US"
    us["listing"] = {"listingId": int(row["listing_id"]), "listingStatus": "ACTIVE"}
    gb = {
        "offerId": "gb-decoy",
        "marketplaceId": "EBAY_GB",
        "listing": {"listingId": "000", "listingStatus": "ACTIVE"},
        "listingId": "000",
        "pricingSummary": {"price": {"value": "1.00"}},
        "availableQuantity": 1,
        "listingDescription": "other marketplace",
        "categoryId": "1",
    }
    ebay = FakeEbay({row["sku"]: item})
    original = ebay.get_offers_by_sku

    def get_offers(sku):
        return [gb, *original(sku)]

    ebay.get_offers_by_sku = get_offers
    ctx = make_ctx(tmp_path, ebay=ebay)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            row["sku"],
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--apply",
            "--push-live",
            "--note",
            "nested listing id",
            "--report",
            str(tmp_path / "nested.json"),
        ],
        ctx,
    )
    assert code == 0, out
    assert report["skus"][0]["status"] != "refused"
    assert any("/offer/" in entry["url"] for entry in ebay.puts)
    assert not ebay.forbidden


def test_push_live_mismatch_and_unpublished_do_not_write(tmp_path):
    row = deepcopy(ROWS[2])
    item = inventory_item(row)
    item["offer"].pop("listingId", None)
    item["offer"]["listing"] = {"listingId": PREFIX, "listingStatus": "ACTIVE"}
    ebay = FakeEbay({row["sku"]: item})
    ctx = make_ctx(tmp_path, ebay=ebay)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            row["sku"],
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--push-live",
            "--report",
            str(tmp_path / "mismatch.json"),
        ],
        ctx,
    )
    assert code == 2
    assert report["skus"][0]["refuse_reason"] == "listing_id_mismatch"
    assert ebay.puts == []
    conn_opt = _optimization(ctx, row["sku"])
    assert "Plywood" in json.dumps(conn_opt)

    item["offer"]["listing"] = {}
    item["offer"].pop("listingId", None)
    ebay2 = FakeEbay({row["sku"]: item})
    ctx2 = make_ctx(tmp_path / "unpublished", ebay=ebay2)
    code2, _out2, report2 = run(
        [
            "--store",
            "main",
            "--sku",
            row["sku"],
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--apply",
            "--push-live",
            "--note",
            "unpublished",
            "--report",
            str(tmp_path / "unpublished.json"),
        ],
        ctx2,
    )
    assert code2 == 2
    assert report2["skus"][0]["refuse_reason"] == "offer_not_published"
    assert ebay2.puts == []


def test_dry_run_does_not_read_offers_or_write(tmp_path):
    row = deepcopy(ROWS[2])
    ebay = FakeEbay({row["sku"]: inventory_item(row)})
    calls = {"n": 0}
    original = ebay.get_offers_by_sku

    def get_offers(sku):
        calls["n"] += 1
        return original(sku)

    ebay.get_offers_by_sku = get_offers
    ctx = make_ctx(tmp_path, ebay=ebay)
    code, _out, report = run(
        [
            "--store",
            "main",
            "--sku",
            row["sku"],
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--report",
            str(tmp_path / "dry.json"),
        ],
        ctx,
    )
    assert code == 0
    assert calls["n"] == 0
    assert ebay.puts == []
    assert report["skus"][0]["status"] == "planned"
    assert "Plywood" in json.dumps(_optimization(ctx, row["sku"]))


def _optimization(ctx, sku: str) -> dict:
    import sqlite3

    conn = sqlite3.connect(ctx.db_path)
    raw = conn.execute(
        "SELECT optimization FROM collected_products WHERE sku=?", (sku,)
    ).fetchone()[0]
    conn.close()
    return json.loads(raw)


# --- Main Color source ------------------------------------------------------


def test_main_color_comma_list_allows_whole_items_only():
    source = {"Main Color": "Beige,Natural Wood"}
    assert _color(source, "Beige").ok is True
    assert _color(source, "Natural Wood").ok is True
    natural = _color(source, "Natural")
    assert natural.ok is False
    assert natural.reason == "aspect_unsourced"
    wood = _color(source, "Wood")
    assert wood.ok is False
    assert wood.reason == "aspect_unsourced"


def test_main_color_space_after_comma_allows_trimmed_item():
    source = {"Main Color": "Beige, Natural Wood"}
    assert _color(source, "Natural Wood").ok is True
    assert _color(source, "Beige").ok is True


def test_light_beige_does_not_source_beige():
    result = _color({"Main Color": "Light Beige"}, "Beige")
    assert result.ok is False
    assert result.reason == "aspect_unsourced"


def test_main_color_black_exact_not_prefix():
    assert _color({"Main Color": "Black"}, "Black").ok is True
    short = _color({"Main Color": "Black"}, "Blac")
    assert short.ok is False
    assert short.reason == "aspect_unsourced"


def test_color_and_main_color_match_either_source():
    attrs = {"Color": "White", "Main Color": "Beige,Natural Wood"}
    assert _color(attrs, "Beige").ok is True
    assert _color(attrs, "Natural Wood").ok is True
    assert _color(attrs, "White").ok is True
    neither = _color(attrs, "Black")
    assert neither.ok is False
    assert neither.reason == "aspect_unsourced"


@pytest.mark.parametrize("raw", ["", None, "   ", "\n\t"])
def test_blank_main_color_is_absent(raw):
    result = _color({"Main Color": raw}, "Beige")
    assert result.ok is False
    assert result.reason == "aspect_no_named_source"


def test_blank_main_color_does_not_hide_another_color_source():
    assert _color({"Color": "Beige", "Main Color": ""}, "Beige").ok is True
    assert _color({"Color": "Beige", "Main Color": None}, "Beige").ok is True
    missing = _color({"Color": "Beige", "Main Color": None}, "White")
    assert missing.reason == "aspect_unsourced"


def test_empty_color_key_stays_a_named_source():
    """PR #53: a present Color key, even blank, is not ``aspect_no_named_source``."""
    result = _color({"Color": ""}, "White")
    assert result.ok is False
    assert result.reason == "aspect_unsourced"


@pytest.mark.parametrize(
    "key",
    [
        "Main Color Family",
        "Secondary Color",
        "Main Colors",
        "Color Main",
        "Premain Color",
        "Main Colorway",
        "Main-Color",
        "Main_Color",
        "main_color",
    ],
)
def test_similar_color_keys_are_not_main_color(key):
    result = _color({key: "Beige"}, "Beige")
    assert result.ok is False
    assert result.reason == "aspect_no_named_source"


@pytest.mark.parametrize("key", ["main color", "MAIN COLOR", "  Main  Color  ", "Main Color\r\n"])
def test_main_color_key_case_and_whitespace(key):
    assert _color({key: "Beige,Natural Wood"}, "Natural Wood").ok is True


def test_main_color_value_case_and_request_whitespace():
    assert _color({"Main Color": "beige"}, "Beige").ok is True
    assert _color({"Main Color": "Beige,Natural Wood"}, " natural wood ").ok is True


def test_main_color_plus_is_not_a_list_separator():
    result = _color({"Main Color": "Black+Gold"}, "Black")
    assert result.ok is False
    assert result.reason == "aspect_unsourced"
    assert _color({"Main Color": "Black+Gold"}, "Black+Gold").ok is True


def test_main_color_in_specs_is_a_source():
    result = _color(attributes={}, specs={"Main Color": "Beige, Natural Wood"}, value="Natural Wood")
    assert result.ok is True


def test_spec_main_color_used_when_attribute_is_blank():
    result = _color(
        {"Main Color": None},
        "Beige",
        specs={"Main Color": "Beige,Natural Wood"},
    )
    assert result.ok is True


def test_literal_none_string_is_a_present_main_color():
    result = _color({"Main Color": "None"}, "Beige")
    assert result.ok is False
    assert result.reason == "aspect_unsourced"


def test_description_beige_is_not_a_color_source_without_named_field():
    result = _color({}, "Beige", description="The cabinet is Beige")
    assert result.reason == "aspect_no_named_source"


def test_main_color_does_not_source_material():
    row = _row({"Main Color": "Wood", "Material": "PU Leather"})
    leather = scd.validate_aspect_value("Material", "Leather", row)
    assert leather.ok is False
    assert leather.reason == "aspect_unsourced"
    assert scd.validate_aspect_value("Material", "PU Leather", row).ok is True
    wood = scd.validate_aspect_value("Material", "Wood", _row({"Main Color": "Wood"}))
    assert wood.reason == "aspect_no_named_source"


def test_existing_named_aspect_rules_still_hold():
    pu = _row({"Material": "PU Leather"})
    assert scd.validate_aspect_value("Material", "Leather", pu).reason == "aspect_unsourced"
    assert scd.validate_aspect_value("Material", "PU Leather", pu).ok is True
    off = _row({"Color": "Off-White"})
    assert scd.validate_aspect_value("Color", "White", off).reason == "aspect_unsourced"
    listed = _row({"Color": "Black/White"})
    assert scd.validate_aspect_value("Color", "Black", listed).ok is True
    assert scd.validate_aspect_value("Color", "White", listed).ok is True
    frame = _row(specs={"Frame Material": "EPP Foam"})
    assert scd.validate_aspect_value("Frame Material", "EPP Foam", frame).ok is True
    assert scd.validate_aspect_value("Frame Material", "Foam", frame).reason == "aspect_unsourced"


def test_set_aspect_color_from_main_color_cli(tmp_path):
    row = draft_row(
        "W2615P429613",
        source_title="Wall cabinet",
        attributes={"Main Color": "Beige,Natural Wood"},
    )
    code, out, report, ctx = run_edit(
        tmp_path / "ok",
        row,
        ["--set-aspect", "Color=Natural Wood"],
    )
    assert code == 0, out
    assert report["skus"][0]["diff"]["aspects"]["after"]["Color"] == ["Natural Wood"]
    saved = _optimization(ctx, row["sku"])
    assert saved["aspects"].get("Color") != ["Natural Wood"]

    refused_code, refused_out, refused_report, _ctx = run_edit(
        tmp_path / "no",
        row,
        ["--set-aspect", "Color=Natural"],
    )
    assert refused_code == 2
    assert "aspect_unsourced" in (refused_out + json.dumps(refused_report))

    missing = draft_row(
        "W2615P429614",
        source_title="Wall cabinet",
        attributes={"Main Color Family": "Beige"},
    )
    missing_code, missing_out, missing_report, _ctx = run_edit(
        tmp_path / "missing",
        missing,
        ["--set-aspect", "Color=Beige"],
    )
    assert missing_code == 2
    assert "aspect_no_named_source" in (missing_out + json.dumps(missing_report))
