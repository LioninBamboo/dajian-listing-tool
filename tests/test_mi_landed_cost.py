from src.plugins.terapeak_research.intelligence_service import landed_cost_from_breakdown


def test_landed_cost_uses_product_price_when_total_dajian_cost_missing():
    cost = {
        "product_price": 100.0,
        "shipping_cost": 0.0,
        "base_cost": 100.0,
        "return_insurance": 2.0,
    }
    value = landed_cost_from_breakdown(cost)
    assert value is not None and value >= 100.0


def test_landed_cost_falls_back_to_base_cost():
    assert landed_cost_from_breakdown({"base_cost": 40.0}) == 40.0


def test_landed_cost_zero_when_empty():
    assert landed_cost_from_breakdown({}) == 0.0
    assert landed_cost_from_breakdown(None) == 0.0
