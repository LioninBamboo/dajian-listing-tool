"""PR #50 review regressions: publish holds, sellable cache, aspect category id."""

from __future__ import annotations

import inspect
import json

import pytest

import batch_publish
from batch_publish import (
    category_hold_publish_result,
    select_batch_publish_category,
)
from src.services.product_router import (
    AUTO,
    classify_product,
    publish_wrong_store_reason,
)
from tests.conftest import load_fixture_category_index
from tests.test_category_route_real_skus_20261028 import BY_SKU, _run

_AQUA_GARAGE_TITLES = (
    "Portable Woodworking Bench Vise",
    "8 Inch Bench Grinder",
    "Rolling Shop Stool",
    "Steel Garage Storage Cabinet",
    "Heavy Duty Work Bench",
)


@pytest.mark.parametrize("title", _AQUA_GARAGE_TITLES)
def test_aqua_garage_furniture_words_are_not_wrong_store(title):
    reason = publish_wrong_store_reason(
        title=title,
        description="This seat cover and floor mat mention a crossbar, radiator, winch, and air compressor.",
        attributes={"Type": "winch", "Feature": "seat cover", "category": "household"},
        store_kind="auto",
    )
    assert reason is None


def test_aqua_still_holds_a_real_sofa():
    assert (
        publish_wrong_store_reason(title="3-Seat Sectional Sofa", store_kind="auto")
        == "wrong_store:furniture"
    )


@pytest.mark.parametrize(
    "title,description",
    [
        ("Wall Mount Clothes Rack with a Crossbar", "Clothes drying rack."),
        ("Clothes Rack Cross Bar for the Closet", ""),
        ("White Wooden Radiator Cover Cabinet", "Covers the heater."),
        ("Adjustable Bar Stool", "Includes a removable seat cover."),
        ("Shiatsu Massage Chair", "The seat cover zips off for cleaning."),
        ("Entryway Hall Tree with a Floor Mat", "Mat stores under the bench."),
    ],
)
def test_main_household_titles_are_not_wrong_store(title, description):
    reason = publish_wrong_store_reason(
        title=title,
        description=description,
        attributes={"Feature": "air compressor", "Extra": "winch and crossbar"},
        supplier_category="home furniture",
        store_kind="furniture",
    )
    assert reason is None


def test_publish_check_ignores_description_and_attribute_auto_words():
    reason = publish_wrong_store_reason(
        title="Oak Hall Tree",
        description="Universal roof rack cargo carrier and trailer hitch.",
        attributes={"Type": "brake pad", "Fitment": "radiator"},
        store_kind="furniture",
    )
    assert reason is None


def test_supplier_category_still_flags_auto_parts_on_main():
    assert (
        publish_wrong_store_reason(
            title="Steel Basket",
            supplier_category="auto parts",
            store_kind="furniture",
        )
        == "wrong_store:auto"
    )


def test_cross_bar_alone_does_not_hold_and_motors_leaf_does():
    assert (
        publish_wrong_store_reason(
            title="Floor Jack Cross Beam Adapter Cross Bar for Jack",
            store_kind="furniture",
        )
        is None
    )
    assert (
        publish_wrong_store_reason(
            title="Floor Jack Cross Beam Adapter Cross Bar for Jack",
            store_kind="furniture",
            category_ids=["262216"],
        )
        == "wrong_store:auto"
    )


def test_fixture_roof_rack_and_floor_jack_stay_wrong_store_on_main():
    roof = BY_SKU["W46540102"]["source_title"]
    assert publish_wrong_store_reason(title=roof, store_kind="furniture") == "wrong_store:auto"
    jack = _run(BY_SKU["W465P370190"])[0]
    assert "wrong_store:auto" in jack["reasons"]
    other = _run(BY_SKU["W46540104"])[0]
    assert "wrong_store:auto" in other["reasons"]
    assert "implausible_category" not in other["reasons"]


def test_real_brake_pad_still_blocks_main():
    assert (
        publish_wrong_store_reason(title="Front Brake Pad and Rotor Kit", store_kind="furniture")
        == "wrong_store:auto"
    )


_ROUTER_LIFT_TITLE = (
    "Router Lift Kit, Aluminum Alloy Router Table Insert Plate with Lifting ..."
)
_AUTO_LIFT_KIT_TITLES = (
    "3 Inch Front Leveling Lift Kit for Silverado",
    "Suspension Lift Kit for Jeep Wrangler JK",
    "2 Inch Body Lift Kit for Ford F150 Truck",
    "Leveling Kit for Chevy",
)


@pytest.mark.parametrize("store_kind", ["furniture", "garden"])
def test_router_lift_kit_is_not_wrong_store_on_main_or_gp(store_kind):
    assert publish_wrong_store_reason(title=_ROUTER_LIFT_TITLE, store_kind=store_kind) is None


@pytest.mark.parametrize("title", _AUTO_LIFT_KIT_TITLES)
def test_auto_lift_kit_is_wrong_store_on_main(title):
    assert (
        publish_wrong_store_reason(title=title, store_kind="furniture") == "wrong_store:auto"
    )


def test_gp_key_conflict_message_keeps_ids_and_suggestion():
    row = BY_SKU["W808P391606"]
    result, opt, attributes, _opt_before, _attr_before = _run(row)
    assert result["action"] == "hold_category_key_conflict"
    payload = category_hold_publish_result(
        result,
        stored_cat=result["original_stored"],
        title=row["source_title"],
        opt=opt,
        attributes=attributes,
        flags=result.get("flags"),
    )
    assert payload["action"] == "hold_category_key_conflict"
    assert payload["suggestion"] == "121851"
    assert "category_key_conflict" in payload["reasons"]
    text = payload["message"]
    assert "54235" in text and "121851" in text
    assert "54235 vs 121851" in text
    assert "category_key_conflict" in text
    assert "suggestion=121851" in text
    assert "not plausible" not in text.lower()
    blob = json.dumps(payload)
    assert "54235" in blob and "121851" in blob
    assert payload.get("conflicts")
    assert "54235" in payload["conflicts"].values()
    assert "121851" in payload["conflicts"].values()


@pytest.mark.parametrize(
    "choice,needle",
    [
        (
            {
                "action": "hold_not_sellable",
                "reasons": ["not_sellable_in_store_tree"],
                "suggestion": "179444",
            },
            "not_sellable_in_store_tree",
        ),
        (
            {
                "action": "hold_wrong_store",
                "reasons": ["wrong_store:auto"],
                "suggestion": None,
            },
            "wrong_store:auto",
        ),
        (
            {
                "action": "hold_implausible",
                "reasons": [
                    "implausible_category",
                    "not_sellable_in_store_tree",
                    "wrong_store:auto",
                ],
                "suggestion": None,
            },
            "wrong_store:auto",
        ),
    ],
)
def test_specific_hold_messages_are_not_the_generic_plausible_text(choice, needle):
    payload = category_hold_publish_result(
        choice,
        stored_cat="262216",
        title="Floor Jack Cross Beam Adapter",
    )
    assert needle in payload["message"]
    assert "not plausible" not in payload["message"].lower()
    assert payload["action"] == choice["action"]
    assert payload["reasons"] == choice["reasons"]
    assert payload["suggestion"] == choice["suggestion"]
    if choice["suggestion"]:
        assert str(choice["suggestion"]) in payload["message"]


def test_implausible_only_keeps_its_own_message():
    payload = category_hold_publish_result(
        {
            "action": "hold_implausible",
            "reasons": ["implausible_category"],
            "suggestion": "33089",
        },
        stored_cat="38221",
        title="Rolling Tool Chest",
    )
    assert "not publishable" in payload["message"]
    assert "not plausible" not in payload["message"].lower()
    assert payload["suggestion"] == "33089"


class _Resp:
    def __init__(self, code, body=None, boom=False):
        self.status_code = code
        self._body = body or {}
        self._boom = boom

    def json(self):
        if self._boom:
            raise RuntimeError("bad json")
        return self._body


def test_sellable_leaf_does_not_cache_transient_failures(monkeypatch):
    cid = "990001"
    batch_publish._CATEGORY_VALIDITY_CACHE.pop(cid, None)

    class _Profile:
        category_tree_id = "0"
        is_motors = False

    monkeypatch.setattr(batch_publish, "get_store_profile", lambda: _Profile())
    calls = {"n": 0}

    def _get(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(503)
        if calls["n"] == 2:
            raise RuntimeError("taxonomy timeout")
        return _Resp(200, {"categorySubtreeNode": {"leafCategoryTreeNode": True}})

    monkeypatch.setattr(batch_publish.requests, "get", _get)

    class _OAuth:
        api_base = "https://api.ebay.com"

        def get_application_token(self):
            return "token"

    oauth = _OAuth()
    first = batch_publish._is_sellable_leaf_category(oauth, cid)
    assert first is None
    assert cid not in batch_publish._CATEGORY_VALIDITY_CACHE
    second = batch_publish._is_sellable_leaf_category(oauth, cid)
    assert second is None
    assert cid not in batch_publish._CATEGORY_VALIDITY_CACHE
    third = batch_publish._is_sellable_leaf_category(oauth, cid)
    assert third is True
    assert batch_publish._CATEGORY_VALIDITY_CACHE[cid] is True
    assert calls["n"] == 3

    missed = "990002"
    batch_publish._CATEGORY_VALIDITY_CACHE.pop(missed, None)

    def _false(*_args, **_kwargs):
        calls["false"] = calls.get("false", 0) + 1
        return _Resp(200, {"categorySubtreeNode": {"leafCategoryTreeNode": False}})

    monkeypatch.setattr(batch_publish.requests, "get", _false)
    assert batch_publish._is_sellable_leaf_category(oauth, missed) is False
    assert batch_publish._CATEGORY_VALIDITY_CACHE[missed] is False
    assert batch_publish._is_sellable_leaf_category(oauth, missed) is False
    assert calls["false"] == 1


def test_unknown_sellable_does_not_hold(monkeypatch):
    cid = "88057"

    def _unknown(_category_id):
        return None

    class _Matcher:
        def canonicalize_category(self, title, category_id, category_name=None, description=None):
            return str(category_id), category_name

        def get_category_and_aspects(self, title, aspects, description=None):
            return cid, "Desks & Tables", dict(aspects or {})

        def is_category_plausible_for_text(self, title, category_id, category_name=None):
            return True

        def _get_category_aspects(self, category_id):
            return [], []

        def _complete_aspects(self, existing, required, recommended, title, category_name, category_id=None):
            return dict(existing or {})

    result = select_batch_publish_category(
        stored_cat=cid,
        stored_cat_name="Desks & Tables",
        title="L-Shaped Computer Desk",
        existing_aspects={"Brand": ["Desk"]},
        opt={"categoryId": cid},
        listing_status="READY",
        category_matcher=_Matcher(),
        sellable_check=_unknown,
        store_kind="furniture",
        index=load_fixture_category_index(),
    )
    assert "not_sellable_in_store_tree" not in result["reasons"]
    assert result["action"] != "hold_not_sellable"

    def _boom(_category_id):
        raise RuntimeError("taxonomy down")

    result = select_batch_publish_category(
        stored_cat=cid,
        stored_cat_name="Desks & Tables",
        title="L-Shaped Computer Desk",
        existing_aspects={"Brand": ["Desk"]},
        opt={"categoryId": cid},
        listing_status="READY",
        category_matcher=_Matcher(),
        sellable_check=_boom,
        store_kind="furniture",
        index=load_fixture_category_index(),
    )
    assert "not_sellable_in_store_tree" not in result["reasons"]
    assert result["action"] != "hold_not_sellable"


def test_complete_aspects_recompute_receives_category_id():
    seen = []

    class _Matcher:
        def canonicalize_category(self, title, category_id, category_name=None, description=None):
            return str(category_id), category_name

        def get_category_and_aspects(self, title, aspects, description=None):
            return "33089", "Tool Boxes", dict(aspects or {})

        def is_category_plausible_for_text(self, title, category_id, category_name=None):
            return True

        def _get_category_aspects(self, category_id):
            return [], []

        def _complete_aspects(self, existing, required, recommended, title, category_name, category_id=None):
            seen.append(category_id)
            return dict(existing or {})

    select_batch_publish_category(
        stored_cat="20487",
        stored_cat_name="Cabinets",
        title="Kitchen Pantry Storage Cabinet",
        existing_aspects={"Brand": ["Home"]},
        opt={"categoryId": "20487"},
        listing_status="READY",
        category_matcher=_Matcher(),
        sellable_check=lambda _cid: True,
        store_kind="furniture",
        index=load_fixture_category_index(),
    )
    assert seen
    assert seen[0] == "20487"


def test_complete_aspects_five_arg_stub_still_runs():
    calls = []

    class _Matcher:
        def canonicalize_category(self, title, category_id, category_name=None, description=None):
            return str(category_id), category_name

        def get_category_and_aspects(self, title, aspects, description=None):
            return "33089", "Tool Boxes", dict(aspects or {})

        def is_category_plausible_for_text(self, title, category_id, category_name=None):
            return True

        def _get_category_aspects(self, category_id):
            return [], []

        def _complete_aspects(self, existing, required, recommended, title, category_name):
            calls.append(category_name)
            return dict(existing or {})

    result = select_batch_publish_category(
        stored_cat="20487",
        stored_cat_name="Cabinets",
        title="Kitchen Pantry Storage Cabinet",
        existing_aspects={"Brand": ["Home"]},
        opt={"categoryId": "20487"},
        listing_status="READY",
        category_matcher=_Matcher(),
        sellable_check=lambda _cid: True,
        store_kind="furniture",
        index=load_fixture_category_index(),
    )
    assert calls
    assert isinstance(result, dict)


def test_classify_product_drops_the_unused_tools_flag():
    assert "tools_route_to_auto" not in inspect.signature(classify_product).parameters
    routed = classify_product(title="Rolling Tool Chest Cabinet")
    assert routed["target"] == AUTO
    assert routed["reason"] == "garage tool term"
