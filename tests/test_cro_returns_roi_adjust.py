"""S76 — returns ROI adjust tests."""
from __future__ import annotations

from src.services.cro_returns_roi_adjust import (
    adjusted_roi, annotate_roi_with_returns, expected_loss_per_unit,
    filter_underperforming,
)


def test_expected_loss_zero_returns():
    assert expected_loss_per_unit(50.0, 0.0) == 0.0


def test_expected_loss_basic():
    # 20% return, $50 price, $2 fee, 100% refund: 0.2 * (50+2) = 10.4
    assert abs(expected_loss_per_unit(50.0, 0.20) - 10.4) < 1e-9


def test_expected_loss_clamps_pct():
    assert expected_loss_per_unit(50, 1.5) == expected_loss_per_unit(50, 1.0)
    assert expected_loss_per_unit(50, -0.5) == 0.0


def test_adjusted_roi_no_ad_spend_no_change():
    out = adjusted_roi(roi=2.0, price=50, sold_units=10,
                       return_pct=0.5, ad_spend=0)
    assert out['adjusted_roi'] == out['original_roi']
    assert out['note'] == 'no_ad_spend'


def test_adjusted_roi_high_returns_drops_below_one():
    # ad_spend=100, roi=1.5 → after returns drops
    out = adjusted_roi(roi=1.5, price=50, sold_units=10,
                       return_pct=0.20, ad_spend=100)
    # loss = 0.2 * (50+2) * 10 = 104; adj = 1.5 - 1.04 = 0.46
    assert out['adjusted_roi'] < 1.0
    assert abs(out['expected_return_loss'] - 104.0) < 1e-6


def test_adjusted_roi_zero_returns_no_change():
    out = adjusted_roi(roi=2.0, price=50, sold_units=10,
                       return_pct=0.0, ad_spend=100)
    assert out['adjusted_roi'] == 2.0


def test_annotate_roi_uses_lookup():
    items = [
        {'sku': 'A', 'roi': 1.5, 'price': 50, 'sold_units': 10,
         'ad_spend': 100},
        {'sku': 'B', 'roi': 1.5, 'price': 50, 'sold_units': 10,
         'ad_spend': 100, 'return_pct': 0.05},
    ]
    out = annotate_roi_with_returns(items, return_lookup={'A': 0.20})
    a = next(i for i in out if i['sku'] == 'A')
    b = next(i for i in out if i['sku'] == 'B')
    assert a['flagged_high_return'] is True
    assert b['flagged_high_return'] is False
    assert a['adjusted_roi'] < b['adjusted_roi']


def test_filter_underperforming():
    items = [{'adjusted_roi': 0.5}, {'adjusted_roi': 1.2},
             {'adjusted_roi': 0.99}]
    out = filter_underperforming(items, min_adjusted_roi=1.0)
    assert len(out) == 2
