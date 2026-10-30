"""Fail-closed category routing for the 2026-10-28 real SKU fixture.

Suggestions are report-only. Nothing here rewrites a category key or publishes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from batch_publish import select_batch_publish_category
from src.services.ebay_category_matcher import EbayCategoryMatcher
from src.services.product_router import (
    AUTO,
    TOOLS_ROUTE_TO_AUTO,
    classify_product,
    pilot_collect_logs,
    publish_wrong_store_reason,
)
from src.utils.category_decision import validate_target_category
from tests.conftest import load_fixture_category_index

_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "category_route_real_skus_20261028.json"
)
ROWS = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
BY_SKU = {row["sku"]: row for row in ROWS}

_BASE_INDEX = dict(load_fixture_category_index())
# These leaves are absent from the shared offline tree. They stay leaves so
# validate can judge the title, and nothing here is a parent of another id.
_BASE_INDEX.update(
    {
        "262216": ("Roof Racks & Cross Bars", "0"),
        "33089": ("Tool Boxes", "0"),
        "178973": ("Cutting Tools", "0"),
        "39731": ("Tool Boxes, Chests", "0"),
        "33088": ("Work Benches", "0"),
        "3489": ("Germany & Colonies", "0"),
        "975": ("Other Collectible Cookware", "0"),
        "184315": ("Caulks & Sealants", "0"),
        "15280": ("Treadmills", "0"),
        "41182": ("Tactical Axes", "0"),
    }
)

# (held, required reason fragments, forbidden fragments, suggestion, rejected ids, kept id)
EXPECT = {
    "W2660P470516": (True, ["implausible_category"], ["wrong_store"], "33089", ["38221"], None),
    "W2660P470523": (True, ["implausible_category"], ["wrong_store"], "33089", ["38221"], None),
    "W46540102": (True, ["wrong_store:auto", "not_sellable_in_store_tree"], ["implausible_category"], None, ["262216"], None),
    "W46540104": (True, ["wrong_store:auto", "not_sellable_in_store_tree"], ["implausible_category"], None, ["262216"], None),
    "W465P370190": (True, ["wrong_store:auto", "not_sellable_in_store_tree"], [], None, ["262216", "41182"], None),
    "W465P146606": (True, ["implausible_category"], ["wrong_store"], None, ["39731"], None),
    "W465P221189": (True, ["implausible_category"], ["wrong_store"], None, ["39731"], None),
    "W465P222640": (True, ["implausible_category"], ["wrong_store"], None, ["39731", "33088"], None),
    "W5288P574616": (True, ["implausible_category"], ["wrong_store"], None, ["184315"], None),
    "W5288P574622": (True, ["implausible_category"], ["wrong_store"], None, ["184315", "3489"], None),
    "W465P408007": (False, [], ["wrong_store", "implausible_category"], None, [], "178973"),
    "N728P338626B": (False, [], ["wrong_store", "implausible_category"], None, [], "15280"),
    "W465P272041": (True, ["implausible_category"], ["wrong_store"], None, ["975"], None),
    "W808P391606": (True, ["category_key_conflict"], ["wrong_store"], "121851", ["54235", "121851"], None),
    "W808P391671": (True, ["category_key_conflict"], ["wrong_store"], "121851", ["54235", "121851"], None),
    "W2660P287608": (True, ["implausible_category"], ["wrong_store"], "179444", ["20487", "179444"], None),
}

_API = {
    "W2660P470516": "33089",
    "W2660P470523": "33089",
    "W46540102": "262216",
    "W46540104": "262216",
    "W465P370190": "41182",
    "W465P146606": "39731",
    "W465P221189": "39731",
    "W465P222640": "33088",
    "W5288P574616": "184315",
    "W5288P574622": "3489",
    "W465P408007": "178973",
    "N728P338626B": "15280",
    "W465P272041": "975",
    "W808P391606": "54235",
    "W808P391671": "54235",
    "W2660P287608": "179444",
}

_STORE_KIND = {"main": "furniture", "grovepop": "furniture", "aquarides": "auto"}
_POISON = {"Certification": ["APEX"], "Grade": ["Gem"], "Quality": ["Mint Hinged"]}


class _StubMatcher:
    def __init__(self, sku: str, store: str):
        self.sku = sku
        self.store = store

    def canonicalize_category(self, title, category_id, category_name=None, description=None):
        cid = str(category_id or "").strip() or None
        if self.store == "aquarides" and cid == "20487":
            return "179444", "Garage/Shop Tool Chests"
        return cid, category_name

    def get_category_and_aspects(self, title, aspects, description=None):
        api = _API[self.sku]
        merged = dict(aspects or {})
        if api == "3489":
            merged.update(_POISON)
        return api, f"Category {api}", merged

    def is_category_plausible_for_text(self, title, category_id, category_name=None):
        from src.utils.category_decision import _category_path, title_vs_category_implausible

        cid = str(category_id or "")
        meta = _BASE_INDEX.get(cid)
        name = meta[0] if isinstance(meta, (list, tuple)) and meta else (category_name or "")
        path = _category_path(cid, index=_BASE_INDEX)
        return title_vs_category_implausible(title, cid, path=path, category_name=name) is None

    def _get_category_aspects(self, category_id):
        if str(category_id) == "3489":
            return (
                [
                    {"name": "Certification", "required": True, "allowed_values": ["APEX"]},
                    {"name": "Grade", "required": True, "allowed_values": ["Gem"]},
                    {"name": "Quality", "required": True, "allowed_values": ["Mint Hinged"]},
                ],
                [],
            )
        return [], []

    def _complete_aspects(self, existing, required, recommended, title, category_name, category_id=None):
        return dict(existing or {})


def _sellable(store: str):
    def check(cid) -> bool:
        cid = str(cid or "")
        if cid in {"262216", "3489", "41182"}:
            return False
        if cid == "179444" and store != "aquarides":
            return False
        return True

    return check


def _run(row, *, tools_route_to_auto=None):
    stored = dict(row["stored_category"] or {})
    opt = {
        "categoryId": stored.get("categoryId"),
        "categoryName": stored.get("categoryName"),
    }
    for key in ("category_id", "ebay_category_id"):
        if stored.get(key):
            opt[key] = stored[key]
    attributes = dict(row.get("source_attributes") or {})
    attributes.update(dict(row.get("attributes_category_keys") or {}))
    opt_before = dict(opt)
    attr_before = dict(attributes)
    result = select_batch_publish_category(
        stored_cat=stored.get("categoryId"),
        stored_cat_name=stored.get("categoryName"),
        title=row["source_title"],
        description=row.get("description_head") or "",
        existing_aspects={"Material": ["Steel"]} if row["sku"] != "W5288P574622" else {},
        opt=opt,
        listing_status=row.get("status") or "READY",
        category_matcher=_StubMatcher(row["sku"], row["store"]),
        sellable_check=_sellable(row["store"]),
        store_kind=_STORE_KIND[row["store"]],
        attributes=attributes,
        tools_route_to_auto=tools_route_to_auto,
        index=_BASE_INDEX,
    )
    return result, opt, attributes, opt_before, attr_before


@pytest.mark.parametrize("row", ROWS, ids=[row["sku"] for row in ROWS])
def test_fixture_row_matches_expected_hold(row):
    held, required, forbidden, suggestion, rejected, kept = EXPECT[row["sku"]]
    result, opt, attributes, opt_before, attr_before = _run(row)
    reasons = [str(item) for item in result["reasons"]]
    assert result["held"] is held
    for piece in required:
        assert any(piece in reason for reason in reasons), (row["sku"], reasons)
    for piece in forbidden:
        assert all(piece not in reason for reason in reasons), (row["sku"], reasons)
    assert result["suggestion"] == suggestion
    assert result["category_id"] not in rejected
    if held:
        assert result["category_id"] is None
    else:
        assert result["category_id"] == kept
    if row["sku"] == "W5288P574622":
        blob = json.dumps(result["aspects"])
        assert "APEX" not in blob and "Gem" not in blob and "Mint Hinged" not in blob
    if row["sku"] == "W2660P470516":
        assert "x-cc=" in (row.get("description_head") or "")
    assert opt == opt_before
    assert attributes == attr_before
    if row["sku"] == "W2660P287608":
        routed = classify_product(title=row["source_title"])
        assert routed["target"] == AUTO
        assert routed["matched"] != "cabinet"


def test_tools_route_flag_defaults_off_and_bolt_cutter_stays_on_main():
    assert TOOLS_ROUTE_TO_AUTO is False
    row = BY_SKU["W465P408007"]
    for flag in (None, False, True):
        result, *_rest = _run(row, tools_route_to_auto=flag)
        assert result["held"] is False
        assert result["category_id"] == "178973"
        assert not any(str(reason).startswith("wrong_store") for reason in result["reasons"])
    assert (
        publish_wrong_store_reason(
            title=row["source_title"],
            store_kind="arttoy",
            tools_route_to_auto=True,
        )
        == "wrong_store:auto"
    )
    assert (
        publish_wrong_store_reason(
            title=row["source_title"],
            store_kind="arttoy",
            tools_route_to_auto=False,
        )
        is None
    )
    assert (
        publish_wrong_store_reason(
            title=row["source_title"],
            store_kind="furniture",
            tools_route_to_auto=True,
        )
        is None
    )


def test_complete_aspects_does_not_invent_stamp_values():
    matcher = EbayCategoryMatcher(type("OAuth", (), {"get_application_token": lambda self: "t"})())
    required = [
        {"name": "Certification", "required": True, "allowed_values": ["APEX"]},
        {"name": "Grade", "required": True, "allowed_values": ["Gem"]},
        {"name": "Quality", "required": True, "allowed_values": ["Mint Hinged"]},
        {"name": "Professional Grader", "required": True, "allowed_values": ["PCGS"]},
    ]
    done = matcher._complete_aspects(
        {},
        required,
        [],
        "Auxiliary products for splicing plastic floors, 30edge sealing strips",
        "Germany & Colonies",
        category_id="3489",
    )
    blob = json.dumps(done).lower()
    assert "apex" not in blob
    assert "gem" not in blob
    assert "mint hinged" not in blob
    outside = matcher._complete_aspects(
        {},
        required,
        [],
        "edge sealing strips for plastic floors",
        "Caulks & Sealants",
        category_id="184315",
    )
    assert outside == {}


def test_x_cc_query_on_tool_chest_description_is_unchanged():
    row = BY_SKU["W2660P470516"]
    original = row["description_head"]
    assert "x-cc=" in original and "x-cu=" in original and "x-ct=" in original and "x-cs=" in original
    _run(row)
    assert row["description_head"] == original


@pytest.fixture
def keyword_matcher(monkeypatch):
    class _Profile:
        is_motors = False
        category_tree_id = "0"
        store_kind = "furniture"
        ebay_marketplace_id = "EBAY_US"
        default_brand = "Generic"

    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: _Profile())
    monkeypatch.setattr("src.services.ebay_category_matcher.get_store_profile", lambda: _Profile())
    return EbayCategoryMatcher(type("OAuth", (), {"get_application_token": lambda self: "t"})())


def test_tool_chest_cabinet_is_not_the_cabinet_catchall(keyword_matcher):
    cid, _name = keyword_matcher._fallback_category("Rolling Metal Tool Chest Cabinet")
    assert cid != "38221"


def test_kitchen_cabinet_with_tool_drawer_still_matches_cabinet(keyword_matcher):
    cid, _name = keyword_matcher._fallback_category("Kitchen Cabinet with Tool Drawer")
    assert cid == "38221"


def test_tool_cabinet_does_not_use_the_cabinet_catchall(keyword_matcher):
    cid, _name = keyword_matcher._fallback_category("Metal Tool Cabinet")
    assert cid != "38221"


def test_floor_jack_cross_bar_is_not_a_roof_rack(keyword_matcher):
    cid, _name = keyword_matcher._fallback_category(
        "1Ton Floor Jack Cross Beam Adapter Cross Bar for Jack"
    )
    assert cid != "262216"


def test_roof_rack_and_truck_bed_cross_bars_match(keyword_matcher):
    assert keyword_matcher._fallback_category("Universal Roof Rack")[0] == "262216"
    assert keyword_matcher._fallback_category("Roof Cargo Basket for Cross Bars")[0] == "262216"
    assert keyword_matcher._fallback_category("Truck Bed Cross Bars")[0] == "262216"


def test_pet_cage_end_table_is_a_dog_crate(keyword_matcher):
    assert keyword_matcher._fallback_category("Wooden Pet Cage End Table")[0] == "121851"


def test_word_boundaries_do_not_match_stable_cabinetry_or_autoclave(keyword_matcher):
    assert keyword_matcher._fallback_category("Heavy Duty Stable Worksurface")[0] != "38204"
    assert keyword_matcher._fallback_category("Oak Cabinetry Set")[0] != "38221"
    assert classify_product(supplier_category="autoclave equipment")["target"] != AUTO
    assert classify_product(supplier_category="automatic feeder")["target"] != AUTO
    assert classify_product(title="Auto Foldable Treadmill")["target"] != AUTO
    assert classify_product(supplier_category="auto parts")["target"] == AUTO


def test_gift_and_stamp_titles_are_not_tool_boxes_or_stamps(keyword_matcher):
    assert keyword_matcher._fallback_category("Mechanic Creeper, Garage Gift, Tool Gift")[0] != "39731"
    cid, _name = keyword_matcher._fallback_category("Stamp Organizer Box")
    assert cid not in {"3489", "39731"}


def test_family_paths_are_implausible_for_the_trap_titles():
    skillet_ok, _reason = validate_target_category(
        "975", "", "12inch Pre-Seasoned Cast Iron Skillet", index=_BASE_INDEX
    )
    assert skillet_ok is False
    strip_ok, _reason = validate_target_category(
        "184315",
        "",
        "Auxiliary products for splicing plastic floors, 30edge sealing strips and 4corners",
        index=_BASE_INDEX,
    )
    assert strip_ok is False
    roof_ok, _reason = validate_target_category("262216", "", "Universal Roof Rack", index=_BASE_INDEX)
    assert roof_ok is True
    jack_ok, _reason = validate_target_category(
        "262216", "", "Floor Jack Cross Beam Adapter", index=_BASE_INDEX
    )
    assert jack_ok is False


def test_router_and_pilot_note_do_not_move_a_tool_chest():
    title = BY_SKU["W2660P287608"]["source_title"]
    logs = pilot_collect_logs(title=title, description="kept", store_kind="furniture")
    assert logs[0] == "pilot_collect_garden"
    assert any(str(item).startswith("ROUTING:") for item in logs)
    assert publish_wrong_store_reason(title=title, store_kind="furniture") is None
    assert publish_wrong_store_reason(title=title, store_kind="auto") is None
    routed = classify_product(title=title)
    assert routed["target"] == AUTO
    assert "cabinet" not in routed["matched"]


def test_keyword_loses_to_a_plausible_api_leaf(keyword_matcher, monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {
                "categorySuggestions": [
                    {"category": {"categoryId": "33089", "categoryName": "Tool Boxes"}}
                ]
            }

    monkeypatch.setattr(
        "src.services.ebay_category_matcher.requests.get",
        lambda *args, **kwargs: _Resp(),
    )
    cid, _name = keyword_matcher._get_best_category("Rolling Metal Tool Chest Cabinet")
    assert cid == "33089"
