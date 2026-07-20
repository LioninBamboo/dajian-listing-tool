"""Tests for the eBay-link collector (B2, blind-box instance)."""

import pytest

from src.clients.ebay_browse_collector import (
    EbayLinkParseError,
    collect_from_url,
    map_to_collected_fields,
    parse_item_id,
    sku_for_item_id,
)


class TestParseItemId:
    @pytest.mark.parametrize(
        "value",
        [
            "https://www.ebay.com/itm/256123456789",
            "https://www.ebay.com/itm/Labubu-Blind-Box/256123456789?hash=item3b",
            "https://www.ebay.com/itm/256123456789?var=987654321012",
            "http://ebay.com/itm/256123456789#desc",
            "v1|256123456789|0",
            "256123456789",
        ],
    )
    def test_extracts_legacy_id(self, value):
        assert parse_item_id(value) == "256123456789"

    def test_query_string_id_does_not_outrank_path_id(self):
        # variation id in the query must not win over the path item id
        assert parse_item_id("https://www.ebay.com/itm/256123456789?var=987654321012") == "256123456789"

    def test_empty_raises(self):
        with pytest.raises(EbayLinkParseError):
            parse_item_id("")

    def test_no_id_raises(self):
        with pytest.raises(EbayLinkParseError):
            parse_item_id("https://www.ebay.com/itm/just-a-slug")

    def test_sku_prefix(self):
        assert sku_for_item_id("256123456789") == "EB-256123456789"


_SAMPLE_ITEM = {
    "itemId": "v1|256123456789|0",
    "legacyItemId": "256123456789",
    "title": "Cute Designer Art Toy Figure",
    "price": {"value": "29.99", "currency": "USD"},
    "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"},
    "additionalImages": [
        {"imageUrl": "https://i.ebayimg.com/b.jpg"},
        {"imageUrl": "https://i.ebayimg.com/a.jpg"},  # duplicate of main
    ],
    "description": "<p>Collectible vinyl figure</p>",
    "condition": "New",
    "localizedAspects": [
        {"name": "Brand", "value": "POP MART"},
        {"name": "Type", "value": "Blind Box"},
        {"name": "Character", "value": "Labubu"},
    ],
    "categoryId": "149372",
    "categoryPath": "Toys & Hobbies|Action Figures",
    "itemLocation": {"country": "CN"},
    "itemWebUrl": "https://www.ebay.com/itm/256123456789",
}


class TestMapping:
    def test_core_fields(self):
        m = map_to_collected_fields(_SAMPLE_ITEM, source_url="https://www.ebay.com/itm/256123456789")
        assert m["sku"] == "EB-256123456789"
        assert m["item_id"] == "256123456789"
        assert m["title"] == "Cute Designer Art Toy Figure"
        assert m["price"] == 29.99
        assert m["condition"] == "New"
        assert m["brand"] == "POP MART"
        assert m["category_id"] == "149372"
        assert m["item_location_country"] == "CN"

    def test_images_deduped_main_first(self):
        m = map_to_collected_fields(_SAMPLE_ITEM)
        assert m["images"] == [
            "https://i.ebayimg.com/a.jpg",
            "https://i.ebayimg.com/b.jpg",
        ]

    def test_aspects_become_attributes(self):
        m = map_to_collected_fields(_SAMPLE_ITEM)
        assert m["attributes"] == {
            "Brand": "POP MART",
            "Type": "Blind Box",
            "Character": "Labubu",
        }

    def test_legacy_id_recovered_from_resource_id_when_missing(self):
        item = dict(_SAMPLE_ITEM)
        item.pop("legacyItemId")
        m = map_to_collected_fields(item)
        assert m["item_id"] == "256123456789"
        assert m["sku"] == "EB-256123456789"

    def test_missing_price_defaults_zero(self):
        item = dict(_SAMPLE_ITEM)
        item.pop("price")
        assert map_to_collected_fields(item)["price"] == 0.0

    def test_short_description_fallback(self):
        item = dict(_SAMPLE_ITEM)
        item.pop("description")
        item["shortDescription"] = "Fallback text"
        assert map_to_collected_fields(item)["description"] == "Fallback text"


class TestCollectFromUrl:
    def test_orchestrates_parse_fetch_map(self, monkeypatch):
        seen = {}

        def fake_fetch(item_id, *, token=None, environment=None):
            seen["item_id"] = item_id
            return _SAMPLE_ITEM

        monkeypatch.setattr(
            "src.clients.ebay_browse_collector.fetch_item", fake_fetch
        )
        result = collect_from_url("https://www.ebay.com/itm/Slug/256123456789?hash=x")
        assert seen["item_id"] == "256123456789"
        assert result["sku"] == "EB-256123456789"
        assert result["url"] == "https://www.ebay.com/itm/Slug/256123456789?hash=x"

    def test_collected_source_banned_terms_are_detectable(self, monkeypatch):
        # B2 + B1 compose: a collected listing carrying "POP MART" must be
        # surfaced by the guard so the operator fixes it before publish.
        from src.utils.banned_terms_guard import scan_listing

        monkeypatch.setattr(
            "src.clients.ebay_browse_collector.fetch_item",
            lambda item_id, **kw: _SAMPLE_ITEM,
        )
        fields = collect_from_url("https://www.ebay.com/itm/256123456789")
        hits = scan_listing(
            title=fields["title"],
            description=fields["description"],
            aspects=fields["attributes"],
            terms=["POP MART"],
        )
        assert any(h.term == "POP MART" for h in hits)
