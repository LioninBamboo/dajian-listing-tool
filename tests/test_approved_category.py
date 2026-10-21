"""Human-approved category lock: publish / normalize / audit must keep it."""

from __future__ import annotations

import batch_publish
from src.utils.approved_category import (
    CATEGORY_APPROVED_KEY,
    evaluate_approved_category,
    is_category_approved,
    mark_category_approved,
)
from src.utils.listing_quality_gate import (
    normalize_generated_listing,
    validate_listing_quality,
)
from src.utils import category_tree_cache

from tests.test_publish_aspect_guards import (
    _FakeCategoryMatcher,
    _product,
    _stub_publish_common,
    _long_description,
)


_APPROVED_LEAF_TREE = {
    "categoryTreeId": "0",
    "categoryTreeVersion": "1",
    "source_host": "https://api.ebay.com",
    "rootCategoryNode": {
        "category": {"categoryId": "0", "categoryName": "Root"},
        "childCategoryTreeNodes": [
            {
                "category": {"categoryId": "11700", "categoryName": "Home & Garden"},
                "childCategoryTreeNodes": [
                    {
                        "category": {
                            "categoryId": "20487",
                            "categoryName": "Cabinets & Cupboards",
                        },
                        "childCategoryTreeNodes": [
                            {
                                "category": {
                                    "categoryId": "176991",
                                    "categoryName": "Bathroom Cabinets",
                                }
                            }
                        ],
                    },
                    {
                        "category": {
                            "categoryId": "183322",
                            "categoryName": "Sideboards & Buffets",
                        }
                    },
                    {
                        "category": {
                            "categoryId": "32878",
                            "categoryName": "Vanities & Makeup Tables",
                        }
                    },
                    {
                        "category": {
                            "categoryId": "115713",
                            "categoryName": "Home Pubs & Bars",
                        }
                    },
                    {
                        "category": {"categoryId": "54235", "categoryName": "Chairs"}
                    },
                    {
                        "category": {"categoryId": "38204", "categoryName": "Tables"}
                    },
                    {
                        "category": {"categoryId": "20748", "categoryName": "Dog Houses"}
                    },
                ],
            }
        ],
    },
}


def _install_leaf_tree(monkeypatch, tmp_path):
    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    category_tree_cache.reset_category_tree_cache()
    monkeypatch.setattr(category_tree_cache, "DEFAULT_CACHE_DIR", cache_dir)
    category_tree_cache.write_category_tree_cache("0", _APPROVED_LEAF_TREE, cache_dir)
    return cache_dir


def _bathroom_opt(*, approved: bool) -> dict:
    opt = {
        "title": "Bathroom Storage Cabinet with Doors and Shelves",
        "description": "<div>Bathroom storage cabinet for towels and toiletries.</div>",
        "categoryId": "176991",
        "categoryName": "Bathroom Cabinets",
        "aspects": {"Brand": ["AquaVerve"]},
    }
    if approved:
        mark_category_approved(opt)
    return opt


def test_operator_marks_approval_on_optimization_json():
    opt = {"categoryId": "176991"}
    assert is_category_approved(opt) is False
    mark_category_approved(opt)
    assert opt[CATEGORY_APPROVED_KEY] is True
    assert is_category_approved(opt) is True


def test_evaluate_preserves_approved_cached_leaf(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    decision = evaluate_approved_category(
        "176991",
        {"categoryId": "176991", CATEGORY_APPROVED_KEY: True},
    )
    assert decision.approved is True
    assert decision.preserve is True
    assert decision.error is None


def test_evaluate_unapproved_does_not_preserve(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    decision = evaluate_approved_category("176991", {"categoryId": "176991"})
    assert decision.approved is False
    assert decision.preserve is False


def test_evaluate_blacklisted_approved_category_is_blocked(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    blacklist = tmp_path / "high_fee.json"
    blacklist.write_text(
        '{"publish_skip_category_ids": ["176991"]}',
        encoding="utf-8",
    )
    decision = evaluate_approved_category(
        "176991",
        {"categoryId": "176991", CATEGORY_APPROVED_KEY: True},
        blacklist_path=str(blacklist),
    )
    assert decision.approved is True
    assert decision.preserve is False
    assert decision.error == "high_insertion_fee"


def test_evaluate_non_leaf_approved_category_has_clear_error(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    decision = evaluate_approved_category(
        "20487",
        {"categoryId": "20487", CATEGORY_APPROVED_KEY: True},
    )
    assert decision.approved is True
    assert decision.preserve is False
    assert "20487" in (decision.error or "")
    assert "not a sellable leaf" in (decision.error or "")


def test_normalize_preserves_approved_bathroom_cabinet():
    normalized = normalize_generated_listing(
        _bathroom_opt(approved=True),
        source_title="Bathroom Storage Cabinet with Doors and Shelves",
        attributes={
            "Assembled Length (in.)": "24",
            "Assembled Width (in.)": "16",
            "Assembled Height (in.)": "32",
        },
        specs={},
        images=["img1", "img2"],
    )
    assert normalized["categoryId"] == "176991"
    assert normalized["categoryName"] == "Bathroom Cabinets"


def test_normalize_still_remaps_unapproved_bathroom_cabinet():
    normalized = normalize_generated_listing(
        _bathroom_opt(approved=False),
        source_title="Bathroom Storage Cabinet with Doors and Shelves",
        attributes={
            "Assembled Length (in.)": "24",
            "Assembled Width (in.)": "16",
            "Assembled Height (in.)": "32",
        },
        specs={},
        images=["img1", "img2"],
    )
    assert normalized["categoryId"] == "20487"
    assert normalized["categoryName"] == "Cabinets & Cupboards"


def test_quality_gate_skips_mismatch_for_approved_category():
    issues = validate_listing_quality(
        _bathroom_opt(approved=True),
        source_title="Bathroom Storage Cabinet with Doors and Shelves",
        images=["img1", "img2"],
    )
    assert not any(issue.code == "category_mismatch" for issue in issues)


class _RemapMatcher(_FakeCategoryMatcher):
    def canonicalize_category(self, title_context, category_id, category_name, description=None):
        if str(category_id) == "176991":
            return "20487", "Cabinets & Cupboards"
        return category_id, category_name

    def get_category_and_aspects(self, title, aspects, description):
        return "20487", "Cabinets & Cupboards", dict(aspects)

    def is_category_plausible_for_text(self, title, category_id, category_name):
        return True


def _publish_bathroom_product(*, approved: bool) -> dict:
    opt = {
        "title": "Bathroom Storage Cabinet with Doors and Shelves",
        "description": _long_description("Bathroom storage cabinet for towels."),
        "categoryId": "176991",
        "categoryName": "Bathroom Cabinets",
        "aspects": {
            "Brand": ["AquaVerve"],
            "Type": ["Cabinet"],
            "Item Length": ["24 in"],
            "Item Width": ["16 in"],
            "Item Height": ["32 in"],
        },
    }
    if approved:
        mark_category_approved(opt)
    return _product(
        title="Bathroom Storage Cabinet with Doors and Shelves",
        description="Bathroom storage cabinet for towels and toiletries.",
        optimization=opt,
    )


def test_batch_publish_preserves_approved_category(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(
        batch_publish,
        "_normalize_generated_listing",
        lambda listing, **kwargs: listing,
    )
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _RemapMatcher(oauth))
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")

    result = batch_publish.publish_single_product(
        _publish_bathroom_product(approved=True),
        dry_run=True,
    )
    assert result["status"] == "dry_run"
    assert result["category_id"] == "176991"


def test_batch_publish_still_normalizes_unapproved_category(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    monkeypatch.setattr(
        batch_publish,
        "_normalize_generated_listing",
        lambda listing, **kwargs: listing,
    )
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _RemapMatcher(oauth))
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")

    result = batch_publish.publish_single_product(
        _publish_bathroom_product(approved=False),
        dry_run=True,
    )
    assert result["status"] == "dry_run"
    assert result["category_id"] == "20487"


def test_batch_publish_blocks_blacklisted_approved_category(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    blacklist = tmp_path / "high_fee.json"
    blacklist.write_text('{"publish_skip_category_ids": ["176991"]}', encoding="utf-8")
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", str(blacklist))
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _RemapMatcher(oauth))

    result = batch_publish.publish_single_product(
        _publish_bathroom_product(approved=True),
        dry_run=True,
    )
    assert result["status"] == "error"
    assert result["message"] == "high_insertion_fee"
    assert result["copy_skip_reasons"] == ["high_insertion_fee"]


def test_batch_publish_rejects_non_leaf_approved_category(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **kwargs: None)
    _stub_publish_common(monkeypatch, matcher_factory=lambda oauth: _RemapMatcher(oauth))

    product = _publish_bathroom_product(approved=True)
    product["optimization"]["categoryId"] = "20487"
    product["optimization"]["categoryName"] = "Cabinets & Cupboards"

    result = batch_publish.publish_single_product(product, dry_run=True)
    assert result["status"] == "error"
    assert "20487" in result["message"]
    assert "not a sellable leaf" in result["message"]


def _load_active_audit():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "audit_fix_active_listings_approved",
        root / "scripts" / "audit_fix_active_listings.py",
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    return audit


class _DogCrateMatcher:
    def canonicalize_category(self, title_context, category_id, category_name, description=None):
        return "38200", "End Tables"

    def is_category_plausible_for_text(self, title, category_id, category_name):
        return False

    def get_category_and_aspects(self, title, aspects, description):
        return "38200", "End Tables", aspects

    def _get_category_aspects(self, category_id):
        return [], []


def _dog_crate_opt(*, category_id: str, category_name: str) -> dict:
    return {
        "title": "Wooden Dog Crate End Table Furniture Kennel",
        "description": "<div>Pet crate that doubles as an end table.</div>",
        "categoryId": category_id,
        "categoryName": category_name,
        "categoryApproved": True,
        "aspects": {"Brand": ["AquaVerve"]},
    }


def test_active_audit_does_not_remap_approved_dog_crate(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    audit = _load_active_audit()
    monkeypatch.setattr(audit, "get_category_matcher", lambda: _DogCrateMatcher())

    issues, fixes = audit.audit_single_product(
        "W808P391671",
        "Wooden Dog Crate End Table Furniture Kennel",
        "{}",
        "{}",
        _dog_crate_opt(category_id="20748", category_name="Dog Houses"),
        "Pet crate furniture",
    )
    assert not any(issue.get("type") == "category_mismatch" for issue in issues)
    assert "categoryId" not in fixes


def test_active_audit_remaps_blacklisted_or_non_leaf_approved_category(monkeypatch, tmp_path):
    _install_leaf_tree(monkeypatch, tmp_path)
    blacklist = tmp_path / "high_fee.json"
    blacklist.write_text('{"publish_skip_category_ids": ["20748"]}', encoding="utf-8")
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", str(blacklist))
    audit = _load_active_audit()
    monkeypatch.setattr(audit, "get_category_matcher", lambda: _DogCrateMatcher())

    blacklisted_issues, blacklisted_fixes = audit.audit_single_product(
        "W808P391671",
        "Wooden Dog Crate End Table Furniture Kennel",
        "{}",
        "{}",
        _dog_crate_opt(category_id="20748", category_name="Dog Houses"),
        "Pet crate furniture",
    )
    assert any(issue.get("type") == "category_mismatch" for issue in blacklisted_issues)
    assert blacklisted_fixes.get("categoryId") == "38200"

    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")
    non_leaf_issues, non_leaf_fixes = audit.audit_single_product(
        "W808P391671",
        "Wooden Dog Crate End Table Furniture Kennel",
        "{}",
        "{}",
        _dog_crate_opt(category_id="11700", category_name="Home & Garden"),
        "Pet crate furniture",
    )
    assert any(issue.get("type") == "category_mismatch" for issue in non_leaf_issues)
    assert non_leaf_fixes.get("categoryId") == "38200"


def test_choose_ready_category_keeps_approved_leaf(monkeypatch, tmp_path):
    from scripts.audit_fix_ready_drafts import choose_ready_category

    _install_leaf_tree(monkeypatch, tmp_path)

    class _Matcher:
        def get_category_and_aspects(self, title, aspects, description):
            return "20487", "Cabinets & Cupboards", aspects

        def canonicalize_category(self, title, category_id, category_name=None, description=None):
            return "20487", "Cabinets & Cupboards"

        def is_category_plausible_for_text(self, title, category_id, category_name=None):
            return category_id == "20487"

    category_id, category_name, _ = choose_ready_category(
        _Matcher(),
        "Bathroom Storage Cabinet",
        "Bathroom Storage Cabinet with Doors",
        "",
        {"Brand": ["AquaVerve"]},
        "176991",
        "Bathroom Cabinets",
        {"categoryId": "176991", CATEGORY_APPROVED_KEY: True},
    )
    assert (category_id, category_name) == ("176991", "Bathroom Cabinets")


def test_choose_ready_category_drops_blacklisted_or_non_leaf_approval(monkeypatch, tmp_path):
    from scripts.audit_fix_ready_drafts import choose_ready_category

    _install_leaf_tree(monkeypatch, tmp_path)
    blacklist = tmp_path / "high_fee.json"
    blacklist.write_text('{"publish_skip_category_ids": ["176991"]}', encoding="utf-8")
    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", str(blacklist))

    class _Matcher:
        def get_category_and_aspects(self, title, aspects, description):
            return "20487", "Cabinets & Cupboards", aspects

        def canonicalize_category(self, title, category_id, category_name=None, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name=None):
            return category_id == "20487"

        def _get_category_aspects(self, category_id):
            return [], []

        def _complete_aspects(self, aspects, required, recommended, title, category_name):
            return aspects

    blacklisted_id, blacklisted_name, _ = choose_ready_category(
        _Matcher(),
        "Bathroom Storage Cabinet",
        "Bathroom Storage Cabinet with Doors",
        "",
        {"Brand": ["AquaVerve"]},
        "176991",
        "Bathroom Cabinets",
        {"categoryId": "176991", CATEGORY_APPROVED_KEY: True},
    )
    assert (blacklisted_id, blacklisted_name) == ("20487", "Cabinets & Cupboards")

    monkeypatch.setenv("HIGH_FEE_CATEGORY_BLACKLIST_PATH", "/nonexistent-high-fee-blacklist.json")
    non_leaf_id, non_leaf_name, _ = choose_ready_category(
        _Matcher(),
        "Bathroom Storage Cabinet",
        "Bathroom Storage Cabinet with Doors",
        "",
        {"Brand": ["AquaVerve"]},
        "11700",
        "Home & Garden",
        {"categoryId": "11700", CATEGORY_APPROVED_KEY: True},
    )
    assert (non_leaf_id, non_leaf_name) == ("20487", "Cabinets & Cupboards")
