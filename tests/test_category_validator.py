"""Category validator, publish gate, and keyword remap tests. HTTP is mocked."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.clients.real_ebay_client import RealEbayClient
from src.services.ebay_category_matcher import (
    EbayCategoryMatcher,
    canonicalize_category,
)
from src.services.ebay_category_validator import CategoryValidator
from src.services.ebay_publisher import EbayPublisher, HUMAN_GATE_ERROR


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


def test_valid_leaf_passes(tmp_path):
    session = TaxonomySession({"22656": (200, leaf_payload("22656", "Coat & Hat Racks"))})
    result = make_validator(tmp_path, session).validate("22656", "0")
    assert result == {"valid": True, "leaf": True, "name": "Coat & Hat Racks", "reason": "leaf"}


def test_non_leaf_3197_remaps_before_publish(tmp_path):
    events = []

    class Validator:
        def validate(self, category_id, tree_id):
            events.append(("validate", str(category_id)))
            if str(category_id) == "3197":
                return {"valid": False, "leaf": False, "name": "Furniture", "reason": "non_leaf"}
            return {"valid": True, "leaf": True, "name": "Coat & Hat Racks", "reason": "leaf"}

    client = SimpleNamespace(
        create_or_replace_inventory_item=lambda **kwargs: events.append("inventory") or {"sku": kwargs["sku"]},
        create_offer=lambda **kwargs: events.append(("create_offer", kwargs["category_id"])) or {"offerId": "O1"},
        update_offer_category=lambda *args: events.append("update"),
        publish_offer=lambda offer_id: events.append("publish") or {"listingId": "L1"},
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "22656", "categoryName": "Coat & Hat Racks"}}
        ],
    )
    product = _product("3197", "Coat Rack solid wood hanger")
    result = EbayPublisher(client, Validator()).publish(product)
    assert result.status == "SUCCESS"
    assert product["optimization"]["categoryId"] == "22656"
    assert any("[CAT_REMAP] 3197→22656" in line for line in product["logs"])
    assert events[0] == ("validate", "3197")
    assert "inventory" in events
    assert ("create_offer", "22656") in events


def test_62005_remaps_to_first_plausible_leaf():
    events = []

    class Validator:
        def validate(self, category_id, tree_id):
            events.append(str(category_id))
            if str(category_id) == "161059":
                return {"valid": True, "leaf": True, "name": "Trash Can Storage", "reason": "leaf"}
            if str(category_id) == "20608":
                return {"valid": True, "leaf": True, "name": "Trash Cans & Wastebaskets", "reason": "leaf"}
            return {"valid": False, "leaf": False, "name": "", "reason": "not_in_tree"}

    client = SimpleNamespace(
        create_or_replace_inventory_item=lambda **kwargs: {"sku": "S"},
        create_offer=lambda **kwargs: {"offerId": "O1", "categoryId": kwargs["category_id"]},
        update_offer_category=lambda *args: None,
        publish_offer=lambda offer_id: {"listingId": "L1"},
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "20608", "categoryName": "Trash Cans & Wastebaskets"}},
            {"category": {"categoryId": "161059", "categoryName": "Trash Can Storage"}},
        ],
    )
    product = _product("999001", "Garbage Bin Shed Stores 2 Trash Cans Metal Outdoor")
    result = EbayPublisher(client, Validator()).publish(product)
    assert result.status == "SUCCESS"
    assert product["optimization"]["categoryId"] == "161059"
    assert "20608" not in events or events.index("161059") > 0
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
    result = EbayPublisher(client, Validator()).publish(product)
    assert result.status == "ERROR"
    assert result.errors == [HUMAN_GATE_ERROR]
    assert called == []
    assert product["status"] == "READY"
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
        create_or_replace_inventory_item=lambda **kwargs: called.append("ebay"),
        create_offer=lambda **kwargs: called.append("ebay"),
        update_offer_category=lambda *a: called.append("ebay"),
        publish_offer=lambda offer_id: called.append("ebay"),
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "22656", "categoryName": "Coat & Hat Racks"}}
        ],
    )
    product = _product("22656", "Coat Rack walnut")
    result = EbayPublisher(client, Validator()).publish(product)
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


def test_both_offer_paths_validate_before_create_or_update():
    events = []

    class Validator:
        def validate(self, category_id, tree_id):
            events.append("validate")
            return {"valid": True, "leaf": True, "name": "Coat & Hat Racks", "reason": "leaf"}

    def client():
        return SimpleNamespace(
            create_or_replace_inventory_item=lambda **kwargs: events.append("inventory") or {},
            create_offer=lambda **kwargs: events.append("create_offer") or {"offerId": "NEW"},
            update_offer_category=lambda *args: events.append("update_offer"),
            publish_offer=lambda offer_id: events.append("publish") or {"listingId": "L"},
            get_category_suggestions=lambda title: [],
        )

    events.clear()
    EbayPublisher(client(), Validator()).publish(_product("22656", "Coat Rack"))
    assert events.index("validate") < events.index("inventory")
    assert events.index("validate") < events.index("create_offer")

    events.clear()
    EbayPublisher(client(), Validator()).publish(_product("22656", "Coat Rack"), existing_offer_id="OLD")
    assert events.index("validate") < events.index("update_offer")
    assert "inventory" not in events
    assert "create_offer" not in events


def test_invalid_category_error_is_not_retried_three_times():
    publish_calls = []

    class Validator:
        def validate(self, category_id, tree_id):
            if str(category_id) == "22656":
                return {"valid": True, "leaf": True, "name": "Coat & Hat Racks", "reason": "leaf"}
            return {"valid": True, "leaf": True, "name": "Old", "reason": "leaf"}

    def publish_offer(offer_id):
        publish_calls.append(offer_id)
        err = RuntimeError("publish failed")
        err.response = FakeResponse(
            400,
            {
                "errors": [
                    {
                        "errorId": 25005,
                        "message": "The unpublished offer has an invalid category ID. The category is not valid, select another category.",
                    }
                ]
            },
        )
        raise err

    updates = []
    client = SimpleNamespace(
        create_or_replace_inventory_item=lambda **kwargs: {},
        create_offer=lambda **kwargs: {"offerId": "O1"},
        update_offer_category=lambda *args: updates.append(args),
        publish_offer=publish_offer,
        get_category_suggestions=lambda title: [
            {"category": {"categoryId": "22656", "categoryName": "Coat & Hat Racks"}}
        ],
    )
    product = _product("111111", "Coat Rack walnut hanger")
    # 111111 is not a retired id, so the pre-check accepts the leaf, then eBay rejects it.
    class AcceptThenRemap(Validator):
        def validate(self, category_id, tree_id):
            return {"valid": True, "leaf": True, "name": "n", "reason": "leaf"}

    result = EbayPublisher(client, AcceptThenRemap()).publish(product)
    assert len(publish_calls) == 2
    assert result.errors == [HUMAN_GATE_ERROR]
    assert product["optimization"]["categoryId"] == "22656"
    assert any("[CAT_REMAP]" in line and "ebay_invalid_category" in line for line in product["logs"])
    assert updates and updates[-1][-1] == "22656"


def test_matcher_keywords_and_canonicalize():
    matcher = EbayCategoryMatcher()
    assert matcher._fallback_category("Solid wood Coat Rack walnut")["categoryId"] == "22656"
    assert matcher._fallback_category("Garbage Bin Shed Stores 2 Trash Cans")["categoryId"] == "161059"
    assert matcher._fallback_category("outdoor deck box resin")["categoryId"] == "184533"
    assert matcher._fallback_category("Outdoor Storage cabinet") is None

    coat = canonicalize_category("32880", "Coat Rack")
    assert coat["categoryId"] == "22656"
    shed = canonicalize_category("42430", "Garbage Bin Shed galvanized")
    assert shed["categoryId"] == "161059"
    deck = canonicalize_category("42430", "Deck Box outdoor")
    assert deck["categoryId"] == "184533"
    assert canonicalize_category("42430", "bolt cutter") is None


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


def test_publisher_does_not_invent_aspects():
    captured = {}

    class Validator:
        def validate(self, category_id, tree_id):
            return {"valid": True, "leaf": True, "name": "Coat & Hat Racks", "reason": "leaf"}

    def create_or_replace_inventory_item(sku, product):
        captured["aspects"] = product["aspects"]
        return {}

    client = SimpleNamespace(
        create_or_replace_inventory_item=create_or_replace_inventory_item,
        create_offer=lambda **kwargs: {"offerId": "O"},
        update_offer_category=lambda *a: None,
        publish_offer=lambda offer_id: {"listingId": "L"},
        get_category_suggestions=lambda title: [],
    )
    product = _product("22656", "Coat Rack")
    product["optimization"].pop("aspects")
    EbayPublisher(client, Validator()).publish(product)
    assert captured["aspects"] == {}
