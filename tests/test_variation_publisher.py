"""Tests for B5 multi-variation publishing (inventory_item_group)."""

import pytest

from src.services.variation_publisher import (
    build_inventory_item_group_payload,
    derive_variation_values,
    publish_variation_group,
)


class TestDeriveValues:
    def test_synthetic_labels_when_no_key(self):
        vals = derive_variation_values([{}, {}, {}], aspect_name="Style")
        assert vals == ["Style 1", "Style 2", "Style 3"]

    def test_uses_distinct_source_values(self):
        vs = [
            {"attributes": {"Character": "Naruto"}},
            {"attributes": {"Character": "Sasuke"}},
        ]
        assert derive_variation_values(vs, label_key="Character") == ["Naruto", "Sasuke"]

    def test_falls_back_when_source_values_not_distinct(self):
        vs = [{"attributes": {"Character": "All"}}, {"attributes": {"Character": "All"}}]
        assert derive_variation_values(vs, label_key="Character", aspect_name="Style") == ["Style 1", "Style 2"]

    def test_dedupes_collisions(self):
        vs = [
            {"attributes": {"C": "A"}},
            {"attributes": {"C": "A"}},
            {"attributes": {"C": "B"}},
        ]
        # A appears twice but B makes the set look distinct enough (3 vs 3? no: 2 distinct < 3)
        out = derive_variation_values(vs, label_key="C", aspect_name="Style")
        assert len(set(out)) == 3  # always unique


class TestGroupPayload:
    def test_shape_and_varies_by(self):
        p = build_inventory_item_group_payload(
            title="Naruto Plush Blind Box Series",
            description="<div>series</div>",
            image_urls=["a.jpg", "b.jpg"],
            common_aspects={"Brand": ["Unbranded"], "Type": ["Plush"]},
            varies_by_aspect="Style",
            variant_skus=["EB-1-a", "EB-1-b"],
            variant_values=["Style 1", "Style 2"],
        )
        assert p["variantSKUs"] == ["EB-1-a", "EB-1-b"]
        assert p["variesBy"]["aspectsImageVariesBy"] == ["Style"]
        assert p["variesBy"]["specifications"][0]["values"] == ["Style 1", "Style 2"]
        assert p["title"] == "Naruto Plush Blind Box Series"

    def test_varying_aspect_stripped_from_common(self):
        p = build_inventory_item_group_payload(
            title="x", description="", image_urls=[],
            common_aspects={"Style": ["should be removed"], "Brand": ["Unbranded"]},
            varies_by_aspect="Style", variant_skus=["s1"], variant_values=["Style 1"],
        )
        assert "Style" not in p["aspects"]
        assert p["aspects"] == {"Brand": ["Unbranded"]}

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            build_inventory_item_group_payload(
                title="x", description="", image_urls=[], common_aspects={},
                varies_by_aspect="Style", variant_skus=["a", "b"], variant_values=["Style 1"],
            )

    def test_title_truncated_to_80(self):
        p = build_inventory_item_group_payload(
            title="X" * 200, description="", image_urls=[], common_aspects={},
            varies_by_aspect="Style", variant_skus=["s"], variant_values=["Style 1"],
        )
        assert len(p["title"]) == 80


class _FakeClient:
    def __init__(self):
        self.items = []
        self.offers = []
        self.groups = []
        self.published = []
        self.marketplace_id = "EBAY_US"

    def create_or_replace_inventory_item(self, sku, product):
        self.items.append((sku, product))
        return {"sku": sku}

    def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
        self.offers.append((sku, price, category_id))
        return {"offerId": f"off-{sku}"}

    def create_or_replace_inventory_item_group(self, group_key, group):
        self.groups.append((group_key, group))
        return {"status": "ok"}

    def publish_by_inventory_item_group(self, group_key, marketplace_id=None):
        self.published.append(group_key)
        return {"listingId": "LISTING123"}


_VARIANTS = [
    {"sku": "EB-9-a", "value": "Style 1", "price": 16.99, "image_urls": ["a.jpg"]},
    {"sku": "EB-9-b", "value": "Style 2", "price": 21.99, "image_urls": ["b.jpg"]},
]
_COMMON = {"Brand": ["Unbranded"], "Type": ["Plush"]}


class TestOrchestration:
    def test_dry_run_builds_without_writes(self):
        c = _FakeClient()
        out = publish_variation_group(
            c, group_key="EB-9", title="Series", description="<div>d</div>",
            image_urls=["cover.jpg"], common_aspects=_COMMON, varies_by_aspect="Style",
            variants=_VARIANTS, category_id="261068", dry_run=True,
        )
        assert out["dry_run"] is True
        assert out["variant_count"] == 2
        assert c.items == [] and c.offers == [] and c.published == []
        # each variant's inventory item carries its own varying value
        skus = {i["sku"]: i for i in out["inventory_items"]}
        assert skus["EB-9-a"]["product"]["aspects"]["Style"] == ["Style 1"]

    def test_live_run_creates_items_offers_group_and_publishes(self):
        c = _FakeClient()
        out = publish_variation_group(
            c, group_key="EB-9", title="Series", description="<div>d</div>",
            image_urls=["cover.jpg"], common_aspects=_COMMON, varies_by_aspect="Style",
            variants=_VARIANTS, category_id="261068", dry_run=False,
        )
        assert len(c.items) == 2
        assert len(c.offers) == 2
        assert c.groups[0][0] == "EB-9"
        assert c.published == ["EB-9"]
        assert out["listingId"] == "LISTING123"

    def test_variant_varying_value_overrides_common(self):
        c = _FakeClient()
        publish_variation_group(
            c, group_key="EB-9", title="S", description="d", image_urls=[],
            common_aspects={"Style": ["ignored"], "Brand": ["Unbranded"]},
            varies_by_aspect="Style", variants=_VARIANTS, dry_run=False,
        )
        # per-item Style is the variant's value, not the common one
        by_sku = {sku: prod for sku, prod in c.items}
        assert by_sku["EB-9-a"]["aspects"]["Style"] == ["Style 1"]
        assert by_sku["EB-9-b"]["aspects"]["Style"] == ["Style 2"]

    def test_empty_variants_raises(self):
        with pytest.raises(ValueError):
            publish_variation_group(
                _FakeClient(), group_key="EB-9", title="S", description="d",
                image_urls=[], common_aspects={}, varies_by_aspect="Style", variants=[],
            )
