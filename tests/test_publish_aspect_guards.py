"""Regression coverage for source-grounded publish aspect behavior."""

from __future__ import annotations

import batch_publish
from src.utils.publish_fee_preview import (
    evaluate_insertion_fee_preview,
    insertion_fee_usd,
    load_high_fee_blacklist_category_paths,
    load_high_fee_category_blacklist,
)
from src.utils.publish_source_aspect_guard import (
    build_giga_source_text,
    find_source_matching_value,
    guard_unsourced_aspects,
    restore_sourced_aspects,
    value_supported_by_source,
)


def test_infer_upholstery_fabric_does_not_invent_a_specific_fiber():
    from src.utils.publish_aspect_completion import infer_upholstery_fabric

    assert infer_upholstery_fabric("Foam Cushion Ottoman", {"Material": "Foam"}) == "Fabric"
    assert infer_upholstery_fabric("Chenille Sofa", {}) == "Chenille"
    assert infer_upholstery_fabric("Microsuede Recliner", {}) == "Microfiber"


def test_specialized_furniture_categories_are_protected_from_generic_remap():
    from src.services.taxonomy_constants import PROTECTED_STORED_CATEGORY_IDS

    assert {"20689", "261263"}.issubset(PROTECTED_STORED_CATEGORY_IDS)


def test_apothecary_cabinet_template_default_is_dropped_when_optional():
    logs = []
    aspects = {"Type": ["Apothecary Cabinet"], "Color": ["White"]}
    source = build_giga_source_text(
        title="Bathroom Vanity with Sink",
        description="A compact bathroom vanity with ceramic basin.",
    )

    blockers = guard_unsourced_aspects(
        aspects,
        source_text=source,
        required_names=[],
        sku="SKU-VANITY",
        log=logs.append,
    )

    assert blockers == []
    assert "Type" not in aspects
    assert any("Apothecary Cabinet" in line and "[ASPECT_STRIP]" in line for line in logs)


def test_apothecary_cabinet_required_without_source_match_blocks():
    aspects = {"Type": ["Apothecary Cabinet"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Generic household widget with no furniture type",
        required_names=["Type"],
        allowed_values_by_name={"Type": ["Apothecary Cabinet"]},
        sku="SKU-WIDGET",
    )

    assert "unsourced_required_aspect:Type" in blockers
    assert "Type" not in aspects


def test_sourced_aspect_is_kept_and_not_overwritten_by_template():
    source = build_giga_source_text(
        title="Bathroom Vanity",
        description="Silver glass bathroom vanity cabinet.",
        attributes={"Color": "Silver", "Material": "Glass"},
    )
    completed = {"Type": ["Apothecary Cabinet"], "Color": ["Red"], "Material": ["Wood"]}
    restore_sourced_aspects(
        completed,
        {"Type": ["Bathroom Vanity"], "Color": ["Silver"], "Material": ["Glass"]},
        source,
    )

    assert completed["Type"] == ["Bathroom Vanity"]
    assert completed["Color"] == ["Silver"]
    assert completed["Material"] == ["Glass"]

    blockers = guard_unsourced_aspects(
        completed,
        source_text=source,
        required_names=["Type", "Color", "Material"],
    )
    assert blockers == []
    assert completed["Type"] == ["Bathroom Vanity"]
    assert completed["Color"] == ["Silver"]
    assert completed["Material"] == ["Glass"]


def test_missing_required_aspect_blocks_when_source_has_no_match():
    aspects = {"Brand": ["AquaVerve"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Plain household widget",
        required_names=["Type"],
    )
    assert blockers == ["unsourced_required_aspect:Type"]


def test_required_unsourced_color_uses_source_matching_value():
    aspects = {"Color": ["Red"]}
    source = "Silver glass wall mirror"
    assert find_source_matching_value("Color", source, ["Red", "Silver", "Black"]) == "Silver"

    blockers = guard_unsourced_aspects(
        aspects,
        source_text=source,
        required_names=["Color"],
        allowed_values_by_name={"Color": ["Red", "Silver", "Black"]},
    )
    assert blockers == []
    assert aspects["Color"] == ["Silver"]


def test_unsourced_voltage_aspect_is_always_dropped():
    aspects = {"Voltage": ["Does Not Apply"], "Type": ["Kitchen Island"]}
    blockers = guard_unsourced_aspects(
        aspects,
        source_text="Kitchen island with wood top and storage drawers",
        required_names=[],
    )
    assert blockers == []
    assert "Voltage" not in aspects
    assert aspects["Type"] == ["Kitchen Island"]


def test_wood_does_not_match_hollywood():
    assert value_supported_by_source("Wood", "Hollywood vanity mirror") is False


def test_red_does_not_match_covered():
    assert value_supported_by_source("Red", "covered patio set") is False


def test_wood_matches_solid_wood():
    assert value_supported_by_source("Wood", "solid wood frame") is True


def test_placeholder_values_are_not_treated_as_sourced():
    source = "Please see the product description for included hardware and assembly."
    assert value_supported_by_source("See Description", source) is False
    assert value_supported_by_source("Refer to description", source) is False

    completed = {"Type": ["Coffee Table"]}
    restore_sourced_aspects(
        completed,
        {
            "Type": ["Coffee Table"],
            "Color": ["See Description"],
            "Features": ["Refer to description"],
        },
        source,
    )
    assert completed["Type"] == ["Coffee Table"]
    assert "Color" not in completed
    assert "Features" not in completed

    completed["Color"] = ["See Description"]
    blockers = guard_unsourced_aspects(
        completed,
        source_text=source,
        required_names=[],
    )
    assert blockers == []
    assert "Color" not in completed


def test_insertion_fee_real_shape_zero_is_allowed():
    reason = evaluate_insertion_fee_preview(
        listing_fees={
            "feeSummaries": [{
                "fees": [{
                    "feeType": "InsertionFee",
                    "amount": {"value": "0.00", "currency": "USD"},
                }]
            }]
        },
        lookup_attempted=True,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    )
    assert reason is None


def test_insertion_fee_real_shape_twenty_blocks():
    reason = evaluate_insertion_fee_preview(
        listing_fees={
            "feeSummaries": [{
                "fees": [{
                    "feeType": "InsertionFee",
                    "amount": {"value": "20.00", "currency": "USD"},
                }]
            }]
        },
        lookup_attempted=True,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    )
    assert reason == "high_insertion_fee"


def test_successful_fee_response_without_insertion_line_counts_as_zero():
    assert insertion_fee_usd({
        "feeSummaries": [{
            "fees": [{
                "feeType": "FinalValueFee",
                "amount": {"value": "3.00", "currency": "USD"},
            }]
        }]
    }) == 0.0
    assert evaluate_insertion_fee_preview(
        listing_fees={"feeSummaries": [{"fees": []}]},
        lookup_attempted=True,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    ) is None


def test_insertion_fee_at_least_20_blocks():
    reason = evaluate_insertion_fee_preview(
        listing_fees={
            "feeSummaries": [{
                "fees": [{
                    "feeType": "INSERTION_FEE",
                    "amount": {"value": "20.00", "currency": "USD"},
                }]
            }]
        },
        lookup_attempted=True,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    )
    assert reason == "high_insertion_fee"


def test_motors_vehicles_trailers_path_blocks_as_high_insertion_fee():
    reason = evaluate_insertion_fee_preview(
        category_id="6001",
        category_path="eBay Motors/Vehicles/Trailers/Cars & Trucks",
        category_name="Cars & Trucks",
        lookup_attempted=False,
    )
    assert reason == "high_insertion_fee"


def test_fee_preview_unavailable_unless_explicitly_allowed(monkeypatch):
    monkeypatch.delenv("ALLOW_PUBLISH_WITHOUT_FEE_PREVIEW", raising=False)
    assert evaluate_insertion_fee_preview(
        lookup_attempted=True,
        lookup_error=RuntimeError("boom"),
    ) == "fee_preview_unavailable"

    monkeypatch.setenv("ALLOW_PUBLISH_WITHOUT_FEE_PREVIEW", "1")
    assert evaluate_insertion_fee_preview(
        lookup_attempted=True,
        lookup_error=RuntimeError("boom"),
    ) is None


def test_high_fee_category_blacklist_json(tmp_path):
    path = tmp_path / "high_fee.json"
    path.write_text('{"categoryIds": ["99999"]}', encoding="utf-8")
    reason = evaluate_insertion_fee_preview(
        category_id="99999",
        blacklist_path=str(path),
        lookup_attempted=False,
    )
    assert reason == "high_insertion_fee"


def test_blacklist_reads_categories_path_entries(tmp_path):
    path = tmp_path / "skip.json"
    path.write_text(
        '{"categories": [{"id": "38204", "path": "Home & Garden > Furniture > Coffee Tables"}]}',
        encoding="utf-8",
    )
    assert load_high_fee_category_blacklist(str(path)) == {"38204"}
    assert load_high_fee_blacklist_category_paths(str(path)) == {
        "38204": "Home & Garden > Furniture > Coffee Tables",
    }
    assert evaluate_insertion_fee_preview(
        category_id="99999",
        category_path="Home & Garden > Furniture > Coffee Tables",
        blacklist_path=str(path),
        lookup_attempted=False,
    ) == "high_insertion_fee"


def test_blacklist_reads_publish_skip_category_ids(tmp_path):
    path = tmp_path / "skip.json"
    path.write_text('{"publish_skip_category_ids": ["1162", "6001"]}', encoding="utf-8")
    assert load_high_fee_category_blacklist(str(path)) == {"1162", "6001"}
    assert evaluate_insertion_fee_preview(
        category_id="1162",
        blacklist_path=str(path),
        lookup_attempted=False,
    ) == "high_insertion_fee"


def test_default_blacklist_path_used_when_env_unset(tmp_path, monkeypatch):
    from src.utils import publish_fee_preview

    data_dir = tmp_path / "data" / "config"
    data_dir.mkdir(parents=True)
    (data_dir / "high_fee_category_blacklist.json").write_text(
        '{"publish_skip_category_ids": ["77777"]}',
        encoding="utf-8",
    )
    monkeypatch.delenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", raising=False)
    monkeypatch.setattr(publish_fee_preview, "REPO_ROOT", tmp_path)
    assert "77777" in load_high_fee_category_blacklist()


def test_empty_blacklist_logs_warning(tmp_path, monkeypatch, caplog):
    import logging
    from src.utils import publish_fee_preview

    monkeypatch.delenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", raising=False)
    monkeypatch.setattr(publish_fee_preview, "REPO_ROOT", tmp_path)
    publish_fee_preview._EMPTY_BLACKLIST_WARNED.clear()
    with caplog.at_level(logging.WARNING):
        assert load_high_fee_category_blacklist() == set()
    assert "empty" in caplog.text.lower()


def test_trailer_hitches_leaf_does_not_block():
    assert evaluate_insertion_fee_preview(
        category_id="33653",
        category_name="Trailer Hitches",
        category_path="eBay Motors / Parts & Accessories / Trailer Hitches",
        lookup_attempted=False,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    ) is None


def test_real_ebay_other_vehicles_and_trailers_path_blocks():
    assert evaluate_insertion_fee_preview(
        category_id="6038",
        category_path="eBay Motors > Other Vehicles & Trailers > Trailers",
        category_name="Trailers",
        lookup_attempted=False,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    ) == "high_insertion_fee"


def test_full_taxonomy_cars_and_trucks_path_blocks():
    assert evaluate_insertion_fee_preview(
        category_id="6001",
        category_path="eBay Motors / Vehicles / Trailers / Cars & Trucks",
        category_name="Cars & Trucks",
        lookup_attempted=False,
        blacklist_path="/nonexistent-high-fee-blacklist.json",
    ) == "high_insertion_fee"


def _long_description(text: str) -> str:
    return (
        f"<div><h3>KEY FEATURES</h3><ul>"
        f"<li>{text}</li>"
        f"<li>Sturdy frame supports everyday household use.</li>"
        f"<li>Compact profile fits apartments and family rooms.</li>"
        f"</ul></div>"
    )


def _product(**overrides):
    product = {
        "sku": "W3204P565677",
        "title": "Coffee Table",
        "description": "Coffee table with storage shelf. Assembly Required Yes.",
        "price": 50.0,
        "suggested_price": 99.0,
        "shipping": 0,
        "images": ["https://example.com/a.jpg"],
        "attributes": {
            "Assembled Length (in.)": "40",
            "Assembled Width (in.)": "20",
            "Assembled Height (in.)": "18",
        },
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 40},
        "optimization": {
            "title": "Coffee Table",
            "description": _long_description("Storage shelf keeps living room essentials organized."),
            "categoryId": "38204",
            "categoryName": "Coffee Tables",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Coffee Table"],
                "Assembly Required": ["Yes"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        },
    }
    product.update(overrides)
    return product


class _FakeOAuth:
    def __init__(self, environment):
        self.environment = environment

    def is_authorized(self):
        return True

    def get_application_token(self):
        return "app-token"

    def get_valid_token(self):
        return "token"


class _FakeCategoryMatcher:
    def __init__(
        self,
        oauth,
        *,
        required=None,
        returned_aspects=None,
        category_id="38204",
        category_name="Coffee Tables",
    ):
        self._required = required or []
        self._returned_aspects = returned_aspects
        self._category_id = category_id
        self._category_name = category_name

    def canonicalize_category(self, title_context, category_id, category_name, description=None):
        return category_id, category_name

    def get_category_and_aspects(self, title, aspects, description):
        completed = dict(self._returned_aspects) if self._returned_aspects is not None else dict(aspects)
        return self._category_id, self._category_name, completed

    def is_category_plausible_for_text(self, title, category_id, category_name):
        return True

    def _get_category_aspects(self, category_id):
        return list(self._required), []


class _FakePolicyManager:
    def __init__(self, oauth):
        pass


def _stub_publish_common(monkeypatch, *, matcher_factory=None, qc_blockers=None):
    monkeypatch.setattr(batch_publish, "enrich_product_dimensions", lambda product: None)
    monkeypatch.setattr(batch_publish, "validate_product", lambda product: [])
    monkeypatch.setattr(batch_publish, "fetch_market_price", lambda title: None)
    monkeypatch.setattr(batch_publish, "calculate_smart_final_price", lambda product, market_p: 99.0)
    monkeypatch.setattr(batch_publish, "record_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "_is_sellable_leaf_category", lambda oauth, category_id: True)
    monkeypatch.setattr(
        batch_publish,
        "run_listing_qc",
        lambda **kwargs: {
            "status": "pass",
            "blockers": list(qc_blockers or []),
            "warnings": [],
            "source_fingerprint": "source",
            "candidate_fingerprint": "candidate",
            "ruleset_version": "listing-qc-v2",
            "fact_sheet_version": 4,
        },
    )
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _FakeOAuth)
    monkeypatch.setattr(
        "src.services.ebay_category_matcher.EbayCategoryMatcher",
        matcher_factory or (lambda oauth: _FakeCategoryMatcher(oauth)),
    )
    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", _FakePolicyManager)


def test_publish_dry_run_does_not_persist_template_aspects(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        batch_publish,
        "_persist_prepared_listing",
        lambda **kwargs: persisted.append(kwargs),
    )
    _stub_publish_common(
        monkeypatch,
        matcher_factory=lambda oauth: _FakeCategoryMatcher(
            oauth,
            returned_aspects={
                "Brand": ["AquaVerve"],
                "Type": ["Apothecary Cabinet"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        ),
    )

    result = batch_publish.publish_single_product(_product(), dry_run=True)

    assert result["status"] == "dry_run"
    assert persisted == []


def test_publish_blocks_unsourced_required_aspect(monkeypatch):
    _stub_publish_common(
        monkeypatch,
        matcher_factory=lambda oauth: _FakeCategoryMatcher(
            oauth,
            required=[{"name": "Type", "required": True, "allowed_values": ["Apothecary Cabinet"]}],
            returned_aspects={
                "Brand": ["AquaVerve"],
                "Type": ["Apothecary Cabinet"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        ),
    )
    product = _product(
        title="Plain household widget",
        description="Plain household widget with no furniture type mentioned.",
        optimization={
            "title": "Plain household widget",
            "description": _long_description("Plain household widget with no furniture type mentioned."),
            "categoryId": "38204",
            "categoryName": "Coffee Tables",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Apothecary Cabinet"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        },
    )

    result = batch_publish.publish_single_product(product, dry_run=True)

    assert result["status"] == "error"
    assert "unsourced_required_aspect:Type" in result["message"]
    assert result["copy_skip_reasons"] == ["unsourced_required_aspect:Type"]


def test_publish_keeps_sourced_type_when_template_tries_to_overwrite(monkeypatch):
    captured = {}
    real_guard = batch_publish.guard_unsourced_aspects

    def _capture(aspects, **kwargs):
        captured["aspects"] = dict(aspects)
        return real_guard(aspects, **kwargs)

    monkeypatch.setattr(batch_publish, "guard_unsourced_aspects", _capture)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    _stub_publish_common(
        monkeypatch,
        matcher_factory=lambda oauth: _FakeCategoryMatcher(
            oauth,
            category_id="32878",
            category_name="Bathroom Vanities",
            required=[{"name": "Type", "required": True, "allowed_values": ["Apothecary Cabinet", "Bathroom Vanity"]}],
            returned_aspects={
                "Brand": ["AquaVerve"],
                "Type": ["Apothecary Cabinet"],
                "Item Length": ["24 in"],
                "Item Width": ["18 in"],
                "Item Height": ["32 in"],
            },
        ),
    )
    product = _product(
        title="Bathroom Vanity with Sink",
        description="Bathroom vanity cabinet with silver glass doors.",
        attributes={
            "Assembled Length (in.)": "24",
            "Assembled Width (in.)": "18",
            "Assembled Height (in.)": "32",
            "Type": "Bathroom Vanity",
        },
        optimization={
            "title": "Bathroom Vanity with Sink",
            "description": _long_description("Bathroom vanity cabinet with silver glass doors."),
            "categoryId": "32878",
            "categoryName": "Bathroom Vanities",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Bathroom Vanity"],
                "Item Length": ["24 in"],
                "Item Width": ["18 in"],
                "Item Height": ["32 in"],
            },
        },
    )

    result = batch_publish.publish_single_product(product, dry_run=True)

    assert result["status"] == "dry_run"
    assert captured["aspects"]["Type"] == ["Bathroom Vanity"]


def test_publish_blocks_when_insertion_fee_is_at_least_20(monkeypatch):
    published = []
    deleted = []

    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            self.published = False

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            return {"status": "success"}

        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            return {"status": "DRAFT"}

        def get_listing_fees(self, offer_id):
            return {
                "feeSummaries": [{
                    "fees": [{
                        "feeType": "InsertionFee",
                        "amount": {"value": "20.00", "currency": "USD"},
                    }]
                }]
            }

        def delete_offer(self, offer_id):
            deleted.append(offer_id)
            return True

        def publish_offer(self, offer_id):
            published.append(offer_id)
            self.published = True
            return {"listingId": "listing-1"}

    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda *args, **kwargs: (None, "ok"))
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _FakeEbayClient)
    _stub_publish_common(monkeypatch)

    result = batch_publish.publish_single_product(_product(), dry_run=False)

    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == ["high_insertion_fee"]
    assert published == []
    assert deleted == ["offer-1"]


def test_publish_blocks_motors_trailers_using_full_category_path(monkeypatch):
    class _Matcher(_FakeCategoryMatcher):
        def get_category_path(self, category_id, category_name="", title=""):
            return "eBay Motors / Vehicles / Trailers / Utility Trailers"

    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _Matcher(oauth))
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")

    result = batch_publish.publish_single_product(_product(), dry_run=True)

    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == ["high_insertion_fee"]


_MOTORS_TRAILER_TREE = {
    "categoryTreeId": "100",
    "categoryTreeVersion": "1",
    "rootCategoryNode": {
        "category": {"categoryId": "6000", "categoryName": "eBay Motors"},
        "childCategoryTreeNodes": [
            {
                "category": {
                    "categoryId": "66468",
                    "categoryName": "Other Vehicles & Trailers",
                },
                "childCategoryTreeNodes": [
                    {"category": {"categoryId": "6038", "categoryName": "Trailers"}},
                ],
            }
        ],
    },
}


def _forbid_taxonomy_tree_http(monkeypatch):
    calls = []

    def _track(url, *args, **kwargs):
        text = str(url)
        calls.append(text)
        if "get_category_subtree" in text or (
            "/category_tree/" in text
            and "get_item_aspects_for_category" not in text
            and "get_category_suggestions" not in text
        ):
            raise AssertionError(f"publish/dry-run must not fetch taxonomy trees: {text}")

        class _Resp:
            status_code = 500

            def json(self):
                return {}

        return _Resp()

    monkeypatch.setattr("requests.get", _track)
    monkeypatch.setattr("src.services.ebay_category_matcher.requests.get", _track)
    monkeypatch.setattr("batch_publish.requests.get", _track)
    return calls


def _cache_backed_matcher(monkeypatch, tmp_path, trees=None):
    from src.services.ebay_category_matcher import EbayCategoryMatcher
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)
    for tree_id, payload in (trees or {"100": _MOTORS_TRAILER_TREE}).items():
        category_tree_cache.write_category_tree_cache(tree_id, payload, cache_dir)

    class _Matcher(_FakeCategoryMatcher):
        def __init__(self, oauth, **kwargs):
            super().__init__(oauth, **kwargs)

        def get_category_path(self, category_id, category_name="", title=""):
            return EbayCategoryMatcher(_FakeOAuth("PRODUCTION")).get_category_path(
                category_id,
                category_name=category_name,
                title=title,
            )

    return _Matcher, cache_dir


def test_publish_dry_run_builds_path_from_cache_without_tree_http(monkeypatch, tmp_path):
    matcher_cls, _ = _cache_backed_matcher(monkeypatch, tmp_path)
    _forbid_taxonomy_tree_http(monkeypatch)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(
        batch_publish,
        "_normalize_generated_listing",
        lambda listing, **kwargs: listing,
    )
    _stub_publish_common(
        monkeypatch,
        matcher_factory=lambda oauth: matcher_cls(
            oauth, category_id="6038", category_name="Trailers"
        ),
    )
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")

    product = _product(
        title="Utility Trailer 12ft Single Axle",
        description="Single-axle utility trailer for cargo.",
        optimization={
            "title": "Utility Trailer 12ft Single Axle",
            "description": _long_description("Single-axle utility trailer for cargo."),
            "categoryId": "6038",
            "categoryName": "Trailers",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Utility Trailer"],
                "Item Length": ["12 ft"],
                "Item Width": ["6 ft"],
                "Item Height": ["3 ft"],
            },
        }
    )
    path = batch_publish.resolve_publish_category_path(
        matcher_cls(_FakeOAuth("PRODUCTION")),
        "6038",
        category_name="Trailers",
    )
    assert path == "eBay Motors > Other Vehicles & Trailers > Trailers"

    result = batch_publish.publish_single_product(product, dry_run=True)
    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == ["high_insertion_fee"]


def test_missing_cache_returns_empty_and_blacklist_by_id_still_blocks(monkeypatch, tmp_path):
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)
    _forbid_taxonomy_tree_http(monkeypatch)

    blacklist = tmp_path / "high_fee.json"
    blacklist.write_text('{"publish_skip_category_ids": ["88888"]}', encoding="utf-8")
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", str(blacklist))

    from src.services.ebay_category_matcher import EbayCategoryMatcher

    class _Matcher(_FakeCategoryMatcher):
        def __init__(self, oauth):
            super().__init__(oauth, category_id="88888", category_name="Widget")

        def get_category_path(self, category_id, category_name="", title=""):
            return EbayCategoryMatcher(_FakeOAuth("PRODUCTION")).get_category_path(
                category_id,
                category_name=category_name,
                title=title,
            )

    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(
        batch_publish,
        "_normalize_generated_listing",
        lambda listing, **kwargs: listing,
    )
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _Matcher(oauth))

    matcher = _Matcher(_FakeOAuth("PRODUCTION"))
    assert matcher.get_category_path("88888", category_name="Widget") == ""
    assert batch_publish.resolve_publish_category_path(
        matcher,
        "88888",
        category_name="Widget",
        blacklist_path=str(blacklist),
    ) == ""

    product = _product(
        title="Household Widget",
        description="Plain household widget for indoor use.",
        optimization={
            "title": "Household Widget",
            "description": _long_description("Plain household widget for indoor use."),
            "categoryId": "88888",
            "categoryName": "Widget",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Widget"],
                "Item Length": ["10 in"],
                "Item Width": ["8 in"],
                "Item Height": ["6 in"],
            },
        }
    )
    result = batch_publish.publish_single_product(product, dry_run=True)
    assert result["status"] == "error"
    assert result["copy_skip_reasons"] == ["high_insertion_fee"]


def test_resolve_path_uses_blacklist_categories_path_when_cache_missing(monkeypatch, tmp_path):
    from src.utils import category_tree_cache

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)

    blacklist = tmp_path / "high_fee.json"
    blacklist.write_text(
        '{"categories": [{"id": "6038", "path": "eBay Motors > Other Vehicles & Trailers > Trailers"}]}',
        encoding="utf-8",
    )

    from src.services.ebay_category_matcher import EbayCategoryMatcher

    class _Matcher(_FakeCategoryMatcher):
        def get_category_path(self, category_id, category_name="", title=""):
            return EbayCategoryMatcher(_FakeOAuth("PRODUCTION")).get_category_path(
                category_id, category_name=category_name, title=title
            )

    path = batch_publish.resolve_publish_category_path(
        _Matcher(_FakeOAuth("PRODUCTION")),
        "6038",
        category_name="Trailers",
        blacklist_path=str(blacklist),
    )
    assert path == "eBay Motors > Other Vehicles & Trailers > Trailers"


def _install_retry_ebay_client(monkeypatch, *, publish_offer):
    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            return ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"]

        def create_or_replace_inventory_item(self, sku, product):
            _FakeEbayClient.inventory_aspects.append(dict(product.get("aspects") or {}))
            return {"status": "success"}

        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            return {"offerId": "offer-1"}

        def get_offer(self, offer_id):
            return {"status": "DRAFT"}

        def get_offers_by_sku(self, sku):
            return []

        def get_listing_fees(self, offer_id):
            return {
                "feeSummaries": [{
                    "fees": [{
                        "feeType": "InsertionFee",
                        "amount": {"value": "0.00", "currency": "USD"},
                    }]
                }]
            }

        def delete_offer(self, offer_id):
            return True

        def publish_offer(self, offer_id):
            return publish_offer(offer_id)

        def get_inventory_item(self, sku):
            return {}

    _FakeEbayClient.inventory_aspects = []
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda *args, **kwargs: (None, "ok"))
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr(batch_publish, "update_product_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(batch_publish.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", _FakeEbayClient)
    return _FakeEbayClient


def test_invalid_category_retry_rechecks_source_and_fee_guards(monkeypatch):
    class _Matcher(_FakeCategoryMatcher):
        def __init__(self, oauth):
            super().__init__(oauth)
            self.calls = 0

        def get_category_and_aspects(self, title, aspects, description):
            self.calls += 1
            if self.calls == 1:
                return "38204", "Coffee Tables", dict(aspects)
            invented = dict(aspects)
            invented["Finish"] = ["Apothecary Cabinet"]
            return "6038", "Trailers", invented

        def _get_category_aspects(self, category_id):
            if str(category_id) == "6038":
                return (
                    [{"name": "Finish", "required": True, "allowed_values": ["Apothecary Cabinet"]}],
                    [],
                )
            return [], []

        def get_category_path(self, category_id, category_name="", title=""):
            if str(category_id) == "6038":
                return "eBay Motors > Other Vehicles & Trailers > Trailers"
            return "Home & Garden > Furniture > Tables > Coffee Tables"

    def _publish(offer_id):
        raise RuntimeError('{"errorId":25005,"message":"invalid category"}')

    _install_retry_ebay_client(monkeypatch, publish_offer=_publish)
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _Matcher(oauth))
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")

    result = batch_publish.publish_single_product(_product(), dry_run=False)

    assert result["status"] == "error"
    assert "unsourced_required_aspect:Finish" in result["message"]
    assert "high_insertion_fee" in result["copy_skip_reasons"]
    assert "unsourced_required_aspect:Finish" in result["copy_skip_reasons"]


def test_invalid_category_retry_strips_template_defaults_before_republish(monkeypatch):
    published = []

    class _Matcher(_FakeCategoryMatcher):
        def __init__(self, oauth):
            super().__init__(oauth)
            self.calls = 0

        def get_category_and_aspects(self, title, aspects, description):
            self.calls += 1
            if self.calls == 1:
                return "38204", "Coffee Tables", dict(aspects)
            invented = dict(aspects)
            invented["Style"] = ["Modern"]
            invented["Color"] = ["See Description"]
            return "99901", "Side Tables", invented

        def _get_category_aspects(self, category_id):
            return [], []

        def get_category_path(self, category_id, category_name="", title=""):
            return "Home & Garden > Furniture > Tables > Side Tables"

    def _publish(offer_id):
        if not published:
            published.append("fail")
            raise RuntimeError("invalid category id")
        published.append(offer_id)
        return {"listingId": "listing-2"}

    client = _install_retry_ebay_client(monkeypatch, publish_offer=_publish)
    monkeypatch.setattr(
        batch_publish,
        "verify_publish_readback",
        lambda *args, **kwargs: {"status": "verified", "passed": True, "issues": []},
    )
    monkeypatch.setattr(batch_publish, "build_publish_readback_expectation", lambda **kwargs: {})
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _Matcher(oauth))
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")

    result = batch_publish.publish_single_product(_product(), dry_run=False)

    assert result["status"] == "success"
    assert published == ["fail", "offer-1"]
    assert len(client.inventory_aspects) == 2
    assert "Style" not in client.inventory_aspects[1]
    assert "Color" not in client.inventory_aspects[1]
    assert client.inventory_aspects[1]["Type"] == ["Coffee Table"]
