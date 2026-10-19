"""validate_product must tolerate SQL NULL cost_breakdown (Python None)."""

import batch_publish


def _product(**overrides):
    product = {
        "sku": "W3204P565677",
        "price": 72.0,
        "suggested_price": None,
        "images": ["https://example.test/img.jpg"],
        "optimization": {
            "title": "Test Sofa",
            "description": "A sofa description",
            "categoryId": "12345",
            "aspects": {},
        },
        "cost_breakdown": None,
    }
    product.update(overrides)
    return product


def test_validate_product_none_cost_breakdown_does_not_raise():
    errors = batch_publish.validate_product(_product(cost_breakdown=None))

    assert isinstance(errors, list)
    assert "no price or cost data to calculate final price" not in errors


def test_validate_product_dict_cost_breakdown_still_requires_price_or_cost():
    missing = batch_publish.validate_product(
        _product(price=0, suggested_price=0, cost_breakdown={})
    )
    assert "no price or cost data to calculate final price" in missing

    present = batch_publish.validate_product(
        _product(
            price=72.0,
            suggested_price=None,
            cost_breakdown={"total_dajian_cost": 40.0},
        )
    )
    assert "no price or cost data to calculate final price" not in present


def test_validate_product_price_below_stored_cost_does_not_block():
    errors = batch_publish.validate_product(
        _product(price=10.0, cost_breakdown={"total_dajian_cost": 80.0})
    )

    assert "no price or cost data to calculate final price" not in errors
