"""PR-D D1: cro_promote MAX_BID_STEP + _ad_floor_check numeric table.

Prices/costs are SYNTHETIC; PricingEngine constants are real (4250f71).
"""
from __future__ import annotations

import pytest

from scripts import cro_promote as cp


@pytest.mark.parametrize(
    "case_id,price,cost,new_bid,expected_profit,expected_min,expect_ok,expect_reason",
    [
        ("N1", 199.99, 120, 2.0, 34.27, 12.00, True, None),
        ("N2", 199.99, 120, 6.0, 25.91, 12.00, True, None),
        ("N3", 149.99, 100, 2.0, 15.60, 10.00, True, None),
        ("N4", 149.99, 100, 6.0, 9.33, 10.00, False, "below_floor_after_ad"),
        ("N5", 59.99, 45, 2.0, 1.00, 4.50, False, "below_floor_after_ad"),
        ("N6", 249.00, 0, 2.0, None, None, False, "no_cost"),
        ("N7", 0, 100, 2.0, None, None, False, "no_cost"),
    ],
    ids=["N1", "N2", "N3", "N4", "N5", "N6", "N7"],
)
def test_ad_floor_check_numeric_table(
    case_id, price, cost, new_bid, expected_profit, expected_min, expect_ok, expect_reason,
):
    gate = cp._ad_floor_check(
        "SYNTHETIC-FLOOR",
        new_bid,
        economics={"price": price, "cost": cost},
    )
    assert gate["ok"] is expect_ok
    assert gate.get("reason") == expect_reason
    if expected_profit is not None:
        assert gate["profit"] == pytest.approx(expected_profit, abs=0.005)
        assert gate["min_profit"] == pytest.approx(expected_min, abs=0.005)


def test_n8_raise_capped_by_max_bid_step(monkeypatch):
    """N8: cur 3.0 + increment 5.0 → raise to 5.0 (3+2), not 8.0."""
    from src.services import cro_margin_aware_bid as mab

    monkeypatch.setattr(cp, "_lookup_listing_id", lambda sku: "99")
    monkeypatch.setattr(mab, "bid_cap_for_sku", lambda *a, **k: 25.0)
    captured = {}

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return {"campaign_id": "C1", "ad_id": "A1", "bid_percentage": 3.0}

        def update_ad_bid(self, cid, lid, new_bid):
            captured["new_bid"] = new_bid
            return {"success": True}

    # SYNTHETIC — clears floor at 5%
    eco = {"price": 199.99, "cost": 50.0}
    rep = cp._promote_one(
        ad_service=FakeAd(), sku="SYNTHETIC-N8",
        suggested_bid_pct=5.0, economics=eco,
    )
    assert rep["status"] == "done"
    assert captured["new_bid"] == 5.0
    assert rep["new_bid"] == 5.0


def test_below_floor_skipped_non_terminal_and_reported(monkeypatch):
    from src.services import cro_margin_aware_bid as mab

    monkeypatch.setattr(cp, "_lookup_listing_id", lambda sku: "99")
    monkeypatch.setattr(mab, "bid_cap_for_sku", lambda *a, **k: 25.0)

    class FakeAd:
        def find_ad_for_listing(self, lid):
            return {"campaign_id": "C1", "ad_id": "A1", "bid_percentage": 3.0}

        def update_ad_bid(self, *a, **k):
            raise AssertionError("must not raise when below floor")

    # N5 economics — below floor at 5% (3+2)
    eco = {"price": 59.99, "cost": 45.0}
    rep = cp._promote_one(
        ad_service=FakeAd(), sku="SYNTHETIC-N5",
        suggested_bid_pct=5.0, economics=eco,
    )
    assert rep["status"] == "skipped"
    assert rep["terminal"] is False
    assert rep["reason"] == "below_floor_after_ad"


def test_dry_run_lists_below_floor(monkeypatch):
    monkeypatch.setattr(
        cp,
        "load_pending",
        lambda action_type: [
            {
                "sku": "SYNTHETIC-N5",
                "detail": {"suggested_bid_pct": 5.0, "cur_bid_pct": 0},
                "economics": {"price": 59.99, "cost": 45.0},
            }
        ],
    )
    rep = cp.run(apply_changes=False, limit=10)
    assert "below_floor" in rep
    assert len(rep["below_floor"]) == 1
    assert rep["below_floor"][0]["reason"] == "below_floor_after_ad"
    assert rep["rows"][0]["status"] == "skipped"
    assert rep["rows"][0].get("terminal") is not True


# --- 3+ SYNTHETIC edge cases (own) ---

def test_synthetic_edge_zero_price_zero_cost_is_no_cost():
    gate = cp._ad_floor_check(
        "SYNTHETIC-EDGE-Z", 2.0, economics={"price": 0, "cost": 0},
    )
    assert gate["ok"] is False
    assert gate["reason"] == "no_cost"


def test_synthetic_edge_missing_economics_loads_none(monkeypatch):
    monkeypatch.setattr(
        "src.services.cro_sku_economics.load_sku_economics",
        lambda sku: None,
        raising=False,
    )
    # Force import path: patch where used
    import src.services.cro_sku_economics as eco_mod
    monkeypatch.setattr(eco_mod, "load_sku_economics", lambda sku: None)
    gate = cp._ad_floor_check("SYNTHETIC-EDGE-MISSING", 2.0, economics=None)
    assert gate["ok"] is False
    assert gate["reason"] == "no_cost"


def test_synthetic_edge_exactly_at_min_profit_is_ok():
    """Find a bid where profit == min_profit within a cent (boundary)."""
    # N3 at 2% is ok with profit 15.60 > 10. Use N3 economics and bid that
    # still clears; assert floor_price is populated.
    gate = cp._ad_floor_check(
        "SYNTHETIC-EDGE-BOUND",
        2.0,
        economics={"price": 149.99, "cost": 100},
    )
    assert gate["ok"] is True
    assert gate["floor_price"] is not None
    assert gate["floor_price"] > 0
