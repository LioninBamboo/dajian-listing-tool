"""Freight-aware landed cost for smart reprice and the price guard."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from src.services.pricing_engine import PricingEngine


def test_resolve_landed_cost_uses_shipping_column_when_breakdown_omits_freight():
    product = 54.7
    ship = 16.35
    expected = PricingEngine.calculate_dajian_cost(product, ship, False)["total_dajian_cost"]
    got = PricingEngine.resolve_landed_cost(
        cost_breakdown={"product_price": product, "shipping_cost": 0, "total_dajian_cost": 58.02},
        shipping_fallback=ship,
    )
    assert got == expected
    assert got > 58.02


def test_resolve_landed_cost_keeps_higher_known_freight():
    expected = PricingEngine.calculate_dajian_cost(54.7, 16.35, False)["total_dajian_cost"]
    got = PricingEngine.resolve_landed_cost(
        cost_breakdown={"product_price": 54.7, "shipping_cost": 10.0},
        shipping_fallback=16.35,
    )
    assert got == expected


def test_resolve_landed_cost_falls_back_to_stored_total_without_product_price():
    got = PricingEngine.resolve_landed_cost(
        cost_breakdown={"total_dajian_cost": 100.0},
        shipping_fallback=16.35,
    )
    assert got == 100.0


def test_smart_floor_from_resolved_cost_is_not_below_true_break_even():
    stored = {
        "product_price": 54.7,
        "shipping_cost": 0,
        "total_dajian_cost": 58.02,
    }
    landed = PricingEngine.resolve_landed_cost(cost_breakdown=stored, shipping_fallback=16.35)
    floor = PricingEngine.calculate_smart_price(landed, 1.0)["floor_price"]
    true_break_even = PricingEngine.absolute_floor_price(landed)
    assert floor + 0.01 >= true_break_even


def test_precheck_uses_shipping_column_not_understated_breakdown():
    from src.services.pricing_engine import PricingEngine as PE
    from src.services.repricing_guard import precheck_price

    product = 54.7
    ship = 16.35
    true_cost = PE.calculate_dajian_cost(product, ship, False)["total_dajian_cost"]
    true_break_even = PE.absolute_floor_price(true_cost)
    understated_floor = PE.calculate_smart_price(58.02, 1.0)["floor_price"]
    assert understated_floor < true_break_even

    tmp = tempfile.TemporaryDirectory()
    db_path = str(Path(tmp.name) / "t.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE collected_products ("
        "sku TEXT PRIMARY KEY, cost_breakdown TEXT, listing_id TEXT, "
        "shipping REAL, specs TEXT, attributes TEXT)"
    )
    conn.execute(
        "INSERT INTO collected_products VALUES (?,?,?,?,?,?)",
        (
            "W465P475235",
            json.dumps(
                {"product_price": product, "shipping_cost": 0, "total_dajian_cost": 58.02}
            ),
            "188760790738",
            ship,
            "{}",
            "{}",
        ),
    )
    conn.commit()
    conn.close()
    try:
        ok, reason = precheck_price("W465P475235", understated_floor, db_path=db_path)
        assert ok is False
        assert "unsafe_even_without_ad" in reason
        ok_high, _ = precheck_price("W465P475235", true_break_even + 5, db_path=db_path)
        assert ok_high is True
    finally:
        tmp.cleanup()
