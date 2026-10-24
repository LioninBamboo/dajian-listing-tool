"""Category validator, publish gate, and keyword remap tests. HTTP is mocked."""

import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_category_matcher import EbayCategoryMatcher
from src.services.ebay_category_validator import CategoryValidator
from src.services.ebay_publisher import (
    HUMAN_GATE_ERROR,
    PublishResult,
    PublishStatus,
    apply_category_publish_gate,
    invalid_category_retry_action,
)
from src.services.vehicle_compatibility import CompatibilityAnalysis


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def leaf_payload(category_id, name, version="v1"):
    return {
        "categoryTreeVersion": version,
        "categorySubtreeNode": {
            "category": {"categoryId": category_id, "categoryName": name},
            "leafCategoryTreeNode": True,
        },
    }


def non_leaf_payload(category_id, name):
    return {
        "categorySubtreeNode": {
            "category": {"categoryId": category_id, "categoryName": name},
            "leafCategoryTreeNode": False,
        }
    }


class TaxonomySession:
    def __init__(self, subtree_by_id, version="v1", boom=False):
        self.subtree_by_id = subtree_by_id
        self.version = version
        self.boom = boom
        self.subtree_calls = []
        self.version_calls = 0

    def get(self, url, headers=None, params=None, timeout=None):
        if self.boom:
            raise OSError("offline")
        if "get_default_category_tree_id" in url:
            self.version_calls += 1
            return FakeResponse(200, {"categoryTreeId": "0", "categoryTreeVersion": self.version})
        cid = str((params or {}).get("category_id"))
        self.subtree_calls.append(cid)
        spec = self.subtree_by_id[cid]
        if spec == "62005":
            return FakeResponse(
                400,
                {"errors": [{"errorId": 62005, "message": "category ID does not belong to specified category tree"}]},
            )
        if spec == "non_leaf":
            return FakeResponse(200, non_leaf_payload(cid, "Furniture"))
        status, payload = spec
        return FakeResponse(status, payload)


def make_validator(tmp_path, session, **kwargs):
    return CategoryValidator(
        session=session,
        token_provider=lambda: "app-token",
        db_path=str(tmp_path / "cache.db"),
        write_cache=kwargs.get("write_cache", True),
        clock=kwargs.get("clock"),
    )


def _matcher():
    matcher = EbayCategoryMatcher.__new__(EbayCategoryMatcher)
    matcher.oauth = None
    matcher.base_url = "https://api.ebay.com"
    matcher._aspects_cache = {}
    return matcher


def test_valid_leaf_passes(tmp_path):
    session = TaxonomySession({"22656": (200, leaf_payload("22656", "Coat & Hat Racks"))})
    result = make_validator(tmp_path, session).validate("22656", "0")
    assert result == {"valid": True, "leaf": True, "name": "Coat & Hat Racks", "reason": "leaf"}


def test_non_leaf_3197_remaps_before_publish():
    calls = []

    class Validator:
        def validate(self, category_id, tree_id):
            calls.append(str(category_id))
            if str(category_id) == "3197":
                return {"valid": False, "leaf": False, "name": "Furniture", "reason": "non_leaf"}
            return {"valid": True, "leaf": True, "name": "Coat & Hat Racks", "reason": "leaf"}

    product = _product("3197", "Coat Rack solid wood hanger")
    aspects = {"Brand": ["FromSource"]}
    client = SimpleNamespace(
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "22656", "categoryName": "Coat & Hat Racks"}}
        ]
    )
    result = apply_category_publish_gate(
        category_id="3197",
        category_name="Furniture",
        title=product["title"],
        product=product,
        aspects=aspects,
        ebay_client=client,
        oauth=None,
        validator=Validator(),
        matcher=_matcher(),
    )
    assert result[0] == "22656"
    assert result[2] == aspects
    assert product["optimization"]["categoryId"] == "22656"
    assert any("[CAT_REMAP] 3197→22656" in line for line in product["logs"])
    assert calls[0] == "3197"


def test_62005_remaps_to_first_plausible_leaf():
    class Validator:
        def validate(self, category_id, tree_id):
            if str(category_id) == "161059":
                return {"valid": True, "leaf": True, "name": "Trash Can Storage", "reason": "leaf"}
            if str(category_id) == "20608":
                return {"valid": True, "leaf": True, "name": "Trash Cans & Wastebaskets", "reason": "leaf"}
            return {"valid": False, "leaf": False, "name": "", "reason": "not_in_tree"}

    product = _product("999001", "Garbage Bin Shed Stores 2 Trash Cans Metal Outdoor")
    aspects = {"Material": ["Steel"]}
    client = SimpleNamespace(
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "20608", "categoryName": "Trash Cans & Wastebaskets"}},
            {"category": {"categoryId": "161059", "categoryName": "Trash Can Storage"}},
        ]
    )
    result = apply_category_publish_gate(
        category_id="999001",
        category_name="Outdoor Storage",
        title=product["title"],
        product=product,
        aspects=aspects,
        ebay_client=client,
        oauth=None,
        validator=Validator(),
        matcher=_matcher(),
    )
    assert result[0] == "161059"
    assert result[2]["Material"] == ["Steel"]
    assert "Type" not in result[2]
    assert any("999001→161059" in line for line in product["logs"])


def test_no_plausible_suggestion_human_gate_skips_ebay():
    called = []

    class Validator:
        def validate(self, category_id, tree_id):
            return {"valid": False, "leaf": False, "name": "", "reason": "not_in_tree"}

    def boom(*args, **kwargs):
        called.append("ebay")
        raise AssertionError("eBay must not be called")

    client = SimpleNamespace(
        create_or_replace_inventory_item=boom,
        create_offer=boom,
        update_offer_category=boom,
        publish_offer=boom,
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "63861", "categoryName": "Wedding Dresses"}}
        ],
    )
    product = _product("42430", "Bolt cutter 18 inch")
    result = apply_category_publish_gate(
        category_id="42430",
        category_name="Outdoor Storage",
        title=product["title"],
        product=product,
        aspects={"Brand": ["FromSource"]},
        ebay_client=client,
        oauth=None,
        validator=Validator(),
        matcher=_matcher(),
    )
    assert isinstance(result, PublishResult)
    assert result.errors == [HUMAN_GATE_ERROR]
    assert called == []
    assert "42430" in product["logs"][-1]
    assert "63861" in product["logs"][-1]


def test_network_error_is_unknown_and_does_not_publish(tmp_path):
    session = TaxonomySession({}, boom=True)
    result = make_validator(tmp_path, session).validate("22656", "0")
    assert result["valid"] is False
    assert result["reason"] == "unknown"

    class Validator:
        def validate(self, category_id, tree_id):
            return {"valid": False, "leaf": False, "name": "", "reason": "unknown"}

    called = []
    client = SimpleNamespace(
        create_offer=lambda **kwargs: called.append("ebay"),
        publish_offer=lambda offer_id: called.append("ebay"),
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "22656", "categoryName": "Coat & Hat Racks"}}
        ],
    )
    product = _product("22656", "Coat Rack walnut")
    result = apply_category_publish_gate(
        category_id="22656",
        category_name="Coat & Hat Racks",
        title=product["title"],
        product=product,
        aspects={},
        ebay_client=client,
        oauth=None,
        validator=Validator(),
        matcher=_matcher(),
    )
    assert isinstance(result, PublishResult)
    assert result.errors == [HUMAN_GATE_ERROR]
    assert called == []


def test_cache_hit_and_ttl(tmp_path):
    now = {"t": datetime(2026, 10, 16, 0, 0, 0)}
    session = TaxonomySession({"22656": (200, leaf_payload("22656", "Coat & Hat Racks"))})
    validator = make_validator(tmp_path, session, clock=lambda: now["t"])
    assert validator.validate("22656", "0")["reason"] == "leaf"
    assert validator.validate("22656", "0")["reason"] == "leaf"
    assert session.subtree_calls == ["22656"]

    now["t"] = now["t"] + timedelta(days=7, seconds=1)
    assert validator.validate("22656", "0")["reason"] == "leaf"
    assert session.subtree_calls == ["22656", "22656"]


def test_tree_version_change_invalidates_cache(tmp_path):
    session = TaxonomySession({"22656": (200, leaf_payload("22656", "Coat & Hat Racks"))}, version="v1")
    validator = make_validator(tmp_path, session)
    validator.validate("22656", "0")
    session.version = "v2"
    validator.validate("22656", "0")
    assert session.subtree_calls == ["22656", "22656"]


def _publisher_for_order(events, existing_offer):
    from src.services.ebay_publisher import EbayPublisher

    pub = EbayPublisher.__new__(EbayPublisher)
    pub.oauth = SimpleNamespace()
    pub.logger = logging.getLogger("category-gate-test")
    pub._category_error_remapped = False
    pub.ebay_client = SimpleNamespace(
        update_offer_category=lambda *args, **kwargs: events.append("update_offer")
    )
    pub._validate_product = lambda product: None
    pub._get_existing_offer = lambda sku: existing_offer
    pub._prepare_publish_data = lambda product: {
        "title": product["title"],
        "aspects": {"Brand": ["FromSource"]},
        "images": ["https://example.test/a.jpg"],
        "description": "desc",
        "price": 81.61,
        "offer_description": "desc",
    }
    pub._get_category_and_aspects = lambda *args, **kwargs: ("22656", "Coat & Hat Racks", {"Brand": ["FromSource"]})

    def gate(*args, **kwargs):
        events.append("validate")
        return "22656", "Coat & Hat Racks", {"Brand": ["FromSource"]}

    pub._apply_category_gate = gate
    pub._prepare_motors_compatibility = lambda *args, **kwargs: CompatibilityAnalysis(mode="not_applicable")
    pub._create_inventory_item = lambda *args, **kwargs: events.append("inventory")
    pub._sync_inventory_compatibility = lambda *args, **kwargs: None
    pub._create_offer = lambda *args, **kwargs: events.append("create_offer") or {"offerId": "NEW"}
    pub._do_publish = lambda *args, **kwargs: events.append("publish") or PublishResult(
        status=PublishStatus.SUCCESS,
        message="ok",
        listing_id="L",
    )
    return pub


def test_both_offer_paths_validate_before_create_or_update():
    from src.services.ebay_publisher import EbayPublisher

    events = []
    product = _product("22656", "Coat Rack")
    product["suggested_price"] = 81.61
    _publisher_for_order(events, None).publish(product)
    assert events.index("validate") < events.index("inventory")
    assert events.index("validate") < events.index("create_offer")

    events.clear()
    existing = {"offerId": "OLD", "status": "UNPUBLISHED", "listing": {}, "categoryId": "32880"}
    _publisher_for_order(events, existing).publish(product)
    assert events.index("validate") < events.index("inventory")
    assert events.index("validate") < events.index("update_offer")
    assert "create_offer" not in events
    assert issubclass(EbayPublisher, EbayPublisher)


def test_invalid_category_error_is_not_retried_three_times():
    assert invalid_category_retry_action(False) == "remap_once"
    assert invalid_category_retry_action(True) == "human_gate"
    actions = [invalid_category_retry_action(i > 0) for i in range(3)]
    assert actions == ["remap_once", "human_gate", "human_gate"]
    assert actions.count("remap_once") == 1


def test_matcher_keywords_and_canonicalize():
    matcher = _matcher()
    assert matcher._fallback_category("Solid wood Coat Rack walnut")[0] == "22656"
    assert matcher._fallback_category("Garbage Bin Shed Stores 2 Trash Cans")[0] == "161059"
    assert matcher._fallback_category("outdoor deck box resin")[0] == "184533"
    outdoor = matcher._fallback_category("Outdoor Storage cabinet")
    assert outdoor[0] != "42430"

    coat = matcher.canonicalize_category("Coat Rack", "32880", "Coat Racks")
    assert coat[0] == "22656"
    shed = matcher.canonicalize_category("Garbage Bin Shed galvanized", "42430", "Outdoor Storage")
    assert shed[0] == "161059"
    deck = matcher.canonicalize_category("Deck Box outdoor", "42430", "Outdoor Storage")
    assert deck[0] == "184533"
    assert matcher.canonicalize_category("bolt cutter", "42430", "Outdoor Storage")[0] is None


def test_client_fallback_never_returns_3197():
    client = RealEbayClient.__new__(RealEbayClient)
    assert client._get_fallback_category("mystery gadget") == []
    assert client._get_fallback_category("Coat Rack walnut")[0]["category"]["categoryId"] == "22656"
    ids = [
        item["category"]["categoryId"]
        for item in client._get_fallback_category("Outdoor Storage something")
    ]
    assert "3197" not in ids
    assert "42430" not in ids
    assert "32880" not in ids


def test_publisher_does_not_invent_aspects():
    product = _product("3197", "Coat Rack walnut hanger")
    aspects = {}

    class Validator:
        def validate(self, category_id, tree_id):
            if str(category_id) == "22656":
                return {"valid": True, "leaf": True, "name": "Coat & Hat Racks", "reason": "leaf"}
            return {"valid": False, "leaf": False, "name": "Furniture", "reason": "non_leaf"}

    client = SimpleNamespace(
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "22656", "categoryName": "Coat & Hat Racks"}}
        ]
    )
    new_id, _name, returned = apply_category_publish_gate(
        category_id="3197",
        category_name="Furniture",
        title=product["title"],
        product=product,
        aspects=aspects,
        ebay_client=client,
        oauth=None,
        validator=Validator(),
        matcher=_matcher(),
    )
    assert new_id == "22656"
    assert returned == {}
    assert "aspects" not in product["optimization"] or product["optimization"].get("aspects") == {"Brand": ["FromSource"]}


def _product(category_id, title):
    return {
        "sku": "SKU1",
        "title": title,
        "description": "desc",
        "image_urls": [],
        "price": 81.61,
        "quantity": 1,
        "optimization": {
            "categoryId": category_id,
            "categoryName": "old",
            "title": title,
            "aspects": {"Brand": ["FromSource"]},
        },
        "logs": [],
        "status": "READY",
    }
