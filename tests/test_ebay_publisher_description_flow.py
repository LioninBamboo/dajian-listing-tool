import logging
from types import SimpleNamespace

from src.services.ebay_publisher import EbayPublisher


def _make_publisher():
    publisher = object.__new__(EbayPublisher)
    publisher.logger = logging.getLogger("test_ebay_publisher_description_flow")
    publisher.EBAY_MOTORS_CATEGORIES = set()
    return publisher


def test_prepare_publish_data_keeps_full_offer_description_but_truncates_inventory_copy(monkeypatch):
    publisher = _make_publisher()
    publisher._process_images = lambda images: ["https://example.com/image.jpg"]

    monkeypatch.setattr(
        "src.services.ebay_publisher.resolve_publish_quantity",
        lambda sku, logger=None: 1,
    )

    long_description = (
        '<div style="max-width:900px;margin:0 auto">'
        '<div><h3>KEY FEATURES</h3><p>' + ("A" * 4200) + "</p></div>"
        '<!-- Specifications Table --><div><h3>SPECIFICATIONS</h3><table><tr><td>Spec</td></tr></table></div>'
        '<div><h3>PACKAGE INCLUDES</h3><p>1 x chaise lounge</p></div>'
        '<div><p>✦ Ships from California, USA ✦</p></div>'
        "</div>"
    )

    product = {
        "sku": "SKU-1",
        "title": "Source title",
        "price": 99.0,
        "images": ["https://example.com/image.jpg"],
        "optimization": {
            "title": "Optimized title",
            "description": long_description,
            "aspects": {"Brand": ["AquaVerve"]},
        },
    }

    publish_data = publisher._prepare_publish_data(product)

    assert publish_data["offer_description"] == long_description
    assert publish_data["description"] == long_description
    assert len(publish_data["inventory_description"]) <= publisher.MAX_DESCRIPTION_LENGTH


def test_create_offer_forwards_listing_description():
    publisher = _make_publisher()
    captured = {}

    class FakeClient:
        def create_offer(self, sku, price, category_id=None, listing_description=None, marketplace_id=None):
            captured["sku"] = sku
            captured["price"] = price
            captured["category_id"] = category_id
            captured["listing_description"] = listing_description
            captured["marketplace_id"] = marketplace_id
            return {"offerId": "offer-1"}

    publisher.ebay_client = FakeClient()

    result = publisher._create_offer("SKU-1", 99.0, "38204", "<div>full description</div>")

    assert result == {"offerId": "offer-1"}
    assert captured["listing_description"] == "<div>full description</div>"


def test_ensure_required_aspects_replaces_unbranded_for_house_brand(monkeypatch):
    publisher = _make_publisher()
    profile = SimpleNamespace(
        brand_name="AquaVerve",
        default_brand="AquaVerve",
        force_house_brand=True,
    )
    monkeypatch.setattr(
        "src.utils.listing_quality_gate._store_profile_or_none",
        lambda: profile,
    )

    completed = publisher._ensure_required_aspects(
        "20487",
        {"Brand": ["Unbranded"]},
        "Tilt Out Trash Cabinet",
        {},
    )

    assert completed["Brand"] == ["AquaVerve"]
