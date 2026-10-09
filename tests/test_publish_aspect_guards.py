"""Regression coverage for source-grounded publish aspect behavior."""

from __future__ import annotations


def test_infer_upholstery_fabric_does_not_invent_a_specific_fiber():
    from src.utils.publish_aspect_completion import infer_upholstery_fabric

    assert infer_upholstery_fabric("Foam Cushion Ottoman", {"Material": "Foam"}) == "Fabric"
    assert infer_upholstery_fabric("Chenille Sofa", {}) == "Chenille"
    assert infer_upholstery_fabric("Microsuede Recliner", {}) == "Microfiber"


def test_specialized_furniture_categories_are_protected_from_generic_remap():
    from src.services.taxonomy_constants import PROTECTED_STORED_CATEGORY_IDS

    assert {"20689", "261263"}.issubset(PROTECTED_STORED_CATEGORY_IDS)
