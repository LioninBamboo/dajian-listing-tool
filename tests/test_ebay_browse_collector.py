"""Tests for the eBay-link collector (B2, blind-box instance)."""

import pytest

from src.clients.ebay_browse_collector import (
    EbayLinkParseError,
    collect_from_url,
    collect_group_from_url,
    map_to_collected_fields,
    parse_item_id,
    parse_variation_id,
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
    "shippingOptions": [
        {"shippingCost": {"value": "9.00", "currency": "USD"}},
        {"shippingCost": {"value": "12.50", "currency": "USD"}},
    ],
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

    def test_captures_cheapest_shipping_and_total_landed(self):
        m = map_to_collected_fields(_SAMPLE_ITEM)
        assert m["shipping"] == 9.00  # cheapest of 9.00 / 12.50
        assert m["total_landed"] == round(29.99 + 9.00, 2)

    def test_missing_shipping_is_free(self):
        item = dict(_SAMPLE_ITEM)
        item.pop("shippingOptions")
        m = map_to_collected_fields(item)
        assert m["shipping"] == 0.0
        assert m["total_landed"] == 29.99

    def test_short_description_fallback(self):
        item = dict(_SAMPLE_ITEM)
        item.pop("description")
        item["shortDescription"] = "Fallback text"
        assert map_to_collected_fields(item)["description"] == "Fallback text"


class TestCollectFromUrl:
    def test_orchestrates_parse_fetch_map(self, monkeypatch):
        seen = {}

        def fake_fetch(item_id, *, variation_id=None, token=None, environment=None):
            seen["item_id"] = item_id
            seen["variation_id"] = variation_id
            return _SAMPLE_ITEM

        monkeypatch.setattr(
            "src.clients.ebay_browse_collector.fetch_item", fake_fetch
        )
        result = collect_from_url("https://www.ebay.com/itm/Slug/256123456789?hash=x")
        assert seen["item_id"] == "256123456789"
        assert result["sku"] == "EB-256123456789"
        assert result["url"] == "https://www.ebay.com/itm/Slug/256123456789?hash=x"

class TestVariations:
    def test_parse_variation_id(self):
        assert parse_variation_id("https://www.ebay.com/itm/376378065234?var=645018887706") == "645018887706"
        assert parse_variation_id("https://www.ebay.com/itm/376378065234") is None
        assert parse_variation_id("376378065234") is None

    def test_sku_includes_variation(self):
        assert sku_for_item_id("376378065234", "645018887706") == "EB-376378065234-645018887706"
        assert sku_for_item_id("376378065234") == "EB-376378065234"

    def test_mapping_extracts_variation_from_resource_id(self):
        item = {"itemId": "v1|376378065234|645018887706", "title": "Fig", "price": {"value": "5.0"}}
        m = map_to_collected_fields(item)
        assert m["variation_id"] == "645018887706"
        assert m["sku"] == "EB-376378065234-645018887706"

    def test_no_variation_when_group_id_zero(self):
        m = map_to_collected_fields(_SAMPLE_ITEM)  # itemId ...|0
        assert m["variation_id"] == ""
        assert m["sku"] == "EB-256123456789"


class TestGroupCollection:
    def _group_items(self, n=3):
        return [
            {
                "itemId": f"v1|376378065234|{645018887705 + i}",
                "legacyItemId": "376378065234",
                "title": "Universal Monsters Series",
                "price": {"value": str(10 + i)},
                "shippingOptions": [{"shippingCost": {"value": "11.99"}}],
                "image": {"imageUrl": f"https://i.ebayimg.com/{i}.jpg"},
                "localizedAspects": [{"name": "Character", "value": f"Figure {i}"}],
            }
            for i in range(n)
        ]

    def test_collects_all_variations(self, monkeypatch):
        items = self._group_items(10)
        monkeypatch.setattr(
            "src.clients.ebay_browse_collector.fetch_item_group",
            lambda item_id, **kw: {"items": items, "commonDescriptions": [{"description": "shared desc"}]},
        )
        g = collect_group_from_url("https://www.ebay.com/itm/376378065234?var=645018887706")
        assert g["variation_count"] == 10
        assert len(g["variations"]) == 10
        assert g["description"] == "shared desc"
        assert len(g["images"]) == 10  # union, deduped
        assert g["variations"][0]["sku"] == "EB-376378065234-645018887705"
        assert g["variations"][0]["shipping"] == 11.99

    def test_falls_back_to_single_when_not_a_group(self, monkeypatch):
        monkeypatch.setattr(
            "src.clients.ebay_browse_collector.fetch_item_group",
            lambda item_id, **kw: {"items": []},
        )
        monkeypatch.setattr(
            "src.clients.ebay_browse_collector.fetch_item",
            lambda item_id, **kw: _SAMPLE_ITEM,
        )
        g = collect_group_from_url("https://www.ebay.com/itm/256123456789")
        assert g["variation_count"] == 1
        assert g["variations"][0]["sku"] == "EB-256123456789"


class TestCollectFromUrlBanned:
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
