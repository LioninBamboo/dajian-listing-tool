"""GIGA order-based procurement cost (not sell price).

Cost basis = GIGA order product + shipping
+ return insurance 2%
+ logistics insurance 3.2% (express) / 5% (freight)
+ Alipay 0.83% on insured subtotal
+ optional Wayfair Net-30 remittance 2% on insured subtotal
"""
from __future__ import annotations

import pytest

from src.services.pricing_engine import PricingEngine


def test_cost_uses_giga_order_not_sell_price():
    """Base must be product+shipping from GIGA order."""
    cost = PricingEngine.calculate_dajian_cost(115.0, 35.94)
    assert cost["cost_basis"] == "giga_order"
    assert cost["giga_order_base"] == pytest.approx(150.94)
    assert cost["base_cost"] == pytest.approx(150.94)
    # Must not equal a sell price like 227.16
    assert cost["total_dajian_cost"] < 200


def test_alipay_and_insurance_on_order_base():
    cost = PricingEngine.calculate_dajian_cost(100.0, 0.0, is_oversize=False)
    # base 100
    # return 2, logistics 3.2 → insured 105.2
    # alipay 105.2 * 0.0083 = 0.87316
    # total 106.07316 → 106.07
    assert cost["return_insurance"] == pytest.approx(2.0)
    assert cost["logistics_insurance"] == pytest.approx(3.2)
    assert cost["insured_subtotal"] == pytest.approx(105.2)
    assert cost["alipay_fee"] == pytest.approx(105.2 * 0.0083, abs=0.0002)
    assert cost["wayfair_net30_fee"] == pytest.approx(0.0)
    assert cost["total_dajian_cost"] == pytest.approx(106.07, abs=0.01)


def test_freight_insurance_rate_for_oversize():
    cost = PricingEngine.calculate_dajian_cost(100.0, 0.0, is_oversize=True)
    assert cost["logistics_insurance"] == pytest.approx(5.0)
    assert cost["logistics_insurance_rate"] == pytest.approx(0.05)


def test_wayfair_net30_fee_two_percent_on_insured():
    cost = PricingEngine.calculate_dajian_cost(
        100.0, 0.0, include_wayfair_net30_fee=True
    )
    # insured 105.2; wayfair 2% = 2.104; alipay still on
    assert cost["wayfair_net30_fee"] == pytest.approx(105.2 * 0.02, abs=0.0002)
    assert cost["include_wayfair_net30_fee"] is True
    expected = 105.2 + 105.2 * 0.0083 + 105.2 * 0.02
    assert cost["total_dajian_cost"] == pytest.approx(round(expected, 2), abs=0.01)


def test_can_disable_alipay_for_isolated_wayfair_mode():
    cost = PricingEngine.calculate_dajian_cost(
        100.0,
        0.0,
        include_alipay_fee=False,
        include_wayfair_net30_fee=True,
    )
    assert cost["alipay_fee"] == pytest.approx(0.0)
    assert cost["wayfair_net30_fee"] == pytest.approx(105.2 * 0.02, abs=0.0002)


def test_w3118_example_giga_order_breakdown():
    """Bench example: GIGA $115 + ship $35.94."""
    cost = PricingEngine.calculate_dajian_cost(115.0, 35.94)
    assert cost["base_cost"] == pytest.approx(150.94)
    # With Alipay only (eBay/GIGA default)
    assert cost["total_dajian_cost"] == pytest.approx(160.11, abs=0.02)

    both = PricingEngine.calculate_dajian_cost(
        115.0, 35.94, include_wayfair_net30_fee=True
    )
    # Extra ~2% of insured subtotal (~158.79 * 0.02 ≈ 3.18)
    assert both["total_dajian_cost"] == pytest.approx(163.28, abs=0.05)
    assert both["total_dajian_cost"] > cost["total_dajian_cost"]
