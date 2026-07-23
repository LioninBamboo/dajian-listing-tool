"""Tests for product routing (which store instance should list a product)."""

import pytest

from src.services.product_router import (
    ARTTOY,
    AUTO,
    FURNITURE,
    UNKNOWN,
    classify_product,
    route_for_product,
)


class TestAuto:
    @pytest.mark.parametrize(
        "title",
        [
            "Black Class 3 Tow Trailer Hitch 2 Inch Receiver",
            "Roof Rack Cargo Basket 50x36 Extendable Rooftop Carrier",
            "Running Board Nerf Bar Side Step for Silverado",
            "3Ton 12V Electric Car Scissor Jack Kit",
            "Front Brake Pad and Rotor Kit",
        ],
    )
    def test_auto_titles_route_to_auto(self, title):
        assert route_for_product(title=title) == AUTO

    def test_supplier_category_beats_title(self):
        # Title alone is vague; the supplier already classified it.
        out = classify_product(title="Universal Steel Basket", supplier_category="Cargo Carriers")
        assert out["target"] == AUTO
        assert out["reason"] == "supplier category"

    def test_reason_is_traceable(self):
        out = classify_product(title="Heavy Duty Tow Hitch Receiver")
        assert out["target"] == AUTO
        assert out["matched"]  # the exact term that decided it


class TestFalseFriends:
    def test_engineered_wood_is_not_automotive(self):
        # "Engineered Wood" contains "engine" — the classic keyword misfire.
        out = classify_product(
            title="Rotatable Bookshelf - White + Engineered Wood + Tempered Glass",
        )
        assert out["target"] == FURNITURE

    def test_pet_stroller_wheels_are_not_automotive(self):
        out = classify_product(title="Double Pet Stroller for Dogs and Cats, 3-in-1 Foldable")
        assert out["target"] != AUTO

    def test_race_car_bed_is_furniture(self):
        out = classify_product(title="Twin Size Race Car Bed Frame - Red + Plywood")
        assert out["target"] == FURNITURE

    def test_bike_trailer_is_not_auto_parts(self):
        assert route_for_product(title="Foldable Bike Trailer Cargo Carrier for Bicycle") != AUTO

    def test_treadmill_running_board_is_not_auto(self):
        # Real misroute: a treadmill's "running board" matched the auto term.
        out = classify_product(
            title="Treadmills for Home, Electric Treadmill with Automatic Incline Running Board"
        )
        assert out["target"] != AUTO

    def test_golf_organizer_is_not_auto(self):
        # Real misroute: a golf caddy matched "trunk organizer".
        out = classify_product(
            title="Golf Bag Organizer for Garage, Wooden Golf Clubs Storage with Trunk Organizer"
        )
        assert out["target"] != AUTO


class TestArtToy:
    def test_blind_box_routes_to_arttoy(self):
        assert route_for_product(title="Designer Vinyl Blind Box Figure Series") == ARTTOY

    def test_arttoy_wins_over_other_signals(self):
        out = classify_product(
            title="Blind Box Mini Figure", supplier_category="Cargo Carriers"
        )
        assert out["target"] == ARTTOY


class TestFurnitureAndUnknown:
    @pytest.mark.parametrize(
        "title",
        [
            "Extendable Dining Table with Rolling Casters",
            "3-Seat Sectional Sofa with Ottoman",
            "53.5in Tall Kitchen Pantry Storage Cabinet",
        ],
    )
    def test_furniture(self, title):
        assert route_for_product(title=title) == FURNITURE

    def test_unknown_when_no_signal(self):
        out = classify_product(title="Assorted Household Item")
        assert out["target"] == UNKNOWN
        assert out["reason"] == "no decisive signal"


class TestInputs:
    def test_attributes_contribute(self):
        out = classify_product(
            title="Universal Fit Accessory",
            attributes={"Type": "Trailer Hitch", "Material": "Steel"},
        )
        assert out["target"] == AUTO

    def test_empty_input_is_unknown(self):
        assert route_for_product(title="", description="") == UNKNOWN

    def test_long_description_is_truncated_safely(self):
        out = classify_product(title="Widget", description="filler " * 5000 + "trailer hitch")
        assert out["target"] in {AUTO, UNKNOWN}  # must not raise
