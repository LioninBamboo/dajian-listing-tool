from src.services.listing_publish_readback import verify_publish_readback


def _expected():
    return {
        "sku": "SKU-1",
        "title": "Solid Wood Side Table",
        "description": "<div>side table</div>",
        "image_urls": ["img-1", "img-2"],
        "video_ids": ["video-1"],
        "quantity": 2,
        "condition": "NEW",
        "package_weight_and_size": {
            "weight": {"value": 10, "unit": "POUND"},
        },
        "category_id": "38204",
        "offer_id": "offer-1",
        "listing_id": "listing-1",
    }


def _inventory(**overrides):
    value = {
        "condition": "NEW",
        "availability": {"shipToLocationAvailability": {"quantity": 2}},
        "product": {
            "title": "Solid Wood Side Table",
            "description": "<div>side table</div>",
            "imageUrls": ["img-1", "img-2"],
            "videoIds": ["video-1"],
            "packageWeightAndSize": {
                "weight": {"value": 10, "unit": "POUND"},
            },
        },
    }
    value.update(overrides)
    return value


def _offer(**overrides):
    value = {
        "offerId": "offer-1",
        "categoryId": "38204",
        "status": "PUBLISHED",
        "listing": {"listingId": "listing-1", "listingStatus": "ACTIVE"},
    }
    value.update(overrides)
    return value


def test_verify_publish_readback_accepts_matching_inventory_and_offer():
    result = verify_publish_readback(_expected(), _inventory(), _offer())

    assert result["status"] == "verified"
    assert result["passed"] is True
    assert result["issues"] == []
    assert result["transport_failures"] == []


def test_verify_publish_readback_normalizes_numeric_package_values():
    result = verify_publish_readback(
        _expected(),
        _inventory(
            product={
                "title": "Solid Wood Side Table",
                "description": "<div>side table</div>",
                "imageUrls": ["img-1", "img-2"],
                "videoIds": ["video-1"],
                "packageWeightAndSize": {
                    "weight": {"value": "10.0", "unit": "POUND"},
                },
            }
        ),
        _offer(),
    )

    assert result["status"] == "verified"


def test_verify_publish_readback_accepts_top_level_inventory_package_data():
    inventory = _inventory()
    package = inventory["product"].pop("packageWeightAndSize")
    inventory["packageWeightAndSize"] = package

    result = verify_publish_readback(_expected(), inventory, _offer())

    assert result["status"] == "verified"
    assert result["passed"] is True


def test_verify_publish_readback_allows_ebay_package_defaults():
    inventory = _inventory()
    package = inventory["product"].pop("packageWeightAndSize")
    package["shippingIrregular"] = False
    inventory["packageWeightAndSize"] = package

    result = verify_publish_readback(_expected(), inventory, _offer())

    assert result["status"] == "verified"
    assert result["passed"] is True


def test_verify_publish_readback_classifies_collapsed_images_as_partial_write():
    result = verify_publish_readback(
        _expected(),
        _inventory(product={"imageUrls": ["img-1"]}),
        _offer(),
    )

    assert result["status"] == "partial_write"
    assert result["passed"] is False
    assert any(issue["code"] == "image_urls_collapsed" for issue in result["issues"])


def test_verify_publish_readback_separates_missing_reads_from_content_mismatch():
    result = verify_publish_readback(_expected(), None, None)

    assert result["status"] == "transport_failure"
    assert result["passed"] is False
    assert {item["code"] for item in result["transport_failures"]} == {
        "inventory_readback_missing",
        "offer_readback_missing",
    }
    assert result["issues"] == []


def test_verify_publish_readback_detects_offer_identity_and_status_drift():
    result = verify_publish_readback(
        _expected(),
        _inventory(),
        _offer(
            offerId="offer-other",
            listing={"listingId": "listing-other", "listingStatus": "ENDED"},
            status="DRAFT",
        ),
    )

    assert result["status"] == "mismatch"
    assert result["passed"] is False
    codes = {item["code"] for item in result["issues"]}
    assert {"offer_id_mismatch", "listing_id_mismatch", "offer_not_live"} <= codes
