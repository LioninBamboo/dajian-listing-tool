"""Sale-price 10% net-margin floor + price-guard raise / pause_ad tests."""
from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.services.pricing_engine import (
    MIN_NET_MARGIN,
    PricingEngine,
    parse_money,
)
from src.services import repricing_guard
from src.services.repricing_guard import PriceGuardResult, precheck_price


FIXTURE_COSTS = (10, 47.13, 230.74, 249.99, 100)
OLD_NINE_ONE_COST = 100.0


def _old_net_over_net_margin(price: float, cost: float, ad_rate=None) -> Decimal:
    """Historical (net − cost) / net formula that logged 9.1% on the old floor."""
    denom = PricingEngine._discount_denom(ad_rate)
    net = Decimal(str(price)) * denom - PricingEngine.FIXED_FEE
    return (net - Decimal(str(cost))) / net


def _seed_db(cost=100.0, sku="SKU-100", listing_id="LID-100") -> str:
    tmp = tempfile.TemporaryDirectory()
    db_path = str(Path(tmp.name) / "t.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE collected_products ("
        "sku TEXT PRIMARY KEY, cost_breakdown TEXT, listing_id TEXT,"
        " shipping REAL, specs TEXT, attributes TEXT)"
    )
    conn.execute(
        "INSERT INTO collected_products VALUES (?, ?, ?, 0, '{}', '{}')",
        (str(sku), json.dumps({"total_dajian_cost": cost}), listing_id),
    )
    conn.commit()
    conn.close()
    # Keep the directory alive for the caller via the path's parent.
    _seed_db._keep = getattr(_seed_db, "_keep", [])
    _seed_db._keep.append(tmp)
    return db_path


class TestFloorYieldsTenPercentOnSalePrice:
    def test_constant_and_default(self):
        assert MIN_NET_MARGIN == Decimal("0.10")
        assert PricingEngine.MIN_NET_MARGIN == Decimal("0.10")
        assert PricingEngine.min_net_margin() == Decimal("0.10")

    @pytest.mark.parametrize("cost", FIXTURE_COSTS)
    @pytest.mark.parametrize("ad_rate", [None, 0.0, 0.05])
    def test_floor_margin_at_least_ten_percent(self, cost, ad_rate):
        floor = PricingEngine.net_margin_floor_price(cost, ad_rate=ad_rate)
        margin = PricingEngine.net_margin_at_price(floor, cost, ad_rate=ad_rate)
        assert margin is not None
        assert margin >= Decimal("0.10")
        assert float(margin) >= 0.10
        # Never publish a 9.99% after rounding.
        assert margin >= Decimal("0.1000")

    def test_old_nine_one_case_fails_old_formula_passes_new(self):
        old_floor = PricingEngine.safe_floor_price(OLD_NINE_ONE_COST, 0.10)
        old_margin = _old_net_over_net_margin(old_floor, OLD_NINE_ONE_COST)
        assert old_margin < Decimal("0.10")
        assert float(old_margin) == pytest.approx(0.091, abs=0.002)

        new_floor = PricingEngine.net_margin_floor_price(OLD_NINE_ONE_COST)
        new_margin = PricingEngine.net_margin_at_price(new_floor, OLD_NINE_ONE_COST)
        assert new_margin >= Decimal("0.10")
        assert new_floor > old_floor

    @pytest.mark.parametrize("cost", FIXTURE_COSTS)
    def test_new_floor_never_below_old_cost_markup_floor(self, cost):
        old_floor = PricingEngine.safe_floor_price(cost, 0.10)
        new_floor = PricingEngine.net_margin_floor_price(cost)
        assert new_floor + 1e-9 >= old_floor


class TestRoundingCeiling:
    def test_floor_rounds_up_never_nine_nine_nine(self):
        cost = Decimal("12.34")
        denom = PricingEngine._discount_denom()
        raw = (cost + PricingEngine.FIXED_FEE) / (denom - Decimal("0.10"))
        half = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ceil = raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        assert half < ceil

        floor = PricingEngine.net_margin_floor_price(cost)
        assert Decimal(str(floor)) == ceil
        margin = PricingEngine.net_margin_at_price(floor, cost)
        assert margin >= Decimal("0.10")
        assert float(margin) >= 0.10
        assert abs(float(margin) - 0.0999) > 0.00005

        half_margin = PricingEngine.net_margin_at_price(float(half), cost)
        assert half_margin < Decimal("0.10")

    def test_exactly_ten_percent_passes(self):
        cost = 100.0
        floor = PricingEngine.net_margin_floor_price(cost)
        margin = PricingEngine.net_margin_at_price(floor, cost)
        assert margin >= Decimal("0.10")
        decision = precheck_price(
            "SKU-100",
            floor,
            total_cost=cost,
            listing_id="L1",
            allow_ad_disable=False,
        )
        assert decision.ok is True
        assert decision.reason == "ok"
        assert decision.pause_ad is False
        assert decision.adjusted_price == pytest.approx(floor)


class TestEnvOverride:
    def test_override_zero_point_twelve(self, monkeypatch):
        monkeypatch.setenv("MIN_NET_MARGIN_PCT", "0.12")
        assert PricingEngine.min_net_margin() == Decimal("0.12")
        floor_12 = PricingEngine.net_margin_floor_price(100)
        monkeypatch.delenv("MIN_NET_MARGIN_PCT")
        floor_10 = PricingEngine.net_margin_floor_price(100)
        assert floor_12 > floor_10
        margin = PricingEngine.net_margin_at_price(floor_12, 100)
        assert margin >= Decimal("0.12")

    def test_invalid_override_falls_back_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("MIN_NET_MARGIN_PCT", "not-a-number")
        with caplog.at_level(logging.WARNING):
            assert PricingEngine.min_net_margin() == Decimal("0.10")
        assert any("MIN_NET_MARGIN_PCT" in rec.message for rec in caplog.records)

    def test_out_of_range_override_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv("MIN_NET_MARGIN_PCT", "12")
        with caplog.at_level(logging.WARNING):
            assert PricingEngine.min_net_margin() == Decimal("0.10")


class TestInvalidInputsRefuse:
    @pytest.mark.parametrize("cost", [None, 0, 0.0, -5, "", "abc", True])
    def test_invalid_cost_refuses_floor(self, cost):
        result = PricingEngine.compute_net_margin_floor(cost)
        assert result["ok"] is False
        assert result["price"] is None
        assert result["reason"]
        with pytest.raises(ValueError):
            PricingEngine.net_margin_floor_price(cost)

    def test_string_cost_is_accepted(self):
        floor = PricingEngine.net_margin_floor_price("47.13")
        assert floor == PricingEngine.net_margin_floor_price(47.13)

    def test_fee_data_missing_refuses(self):
        with patch.object(
            PricingEngine, "_discount_denom", side_effect=ValueError("no fee table")
        ):
            result = PricingEngine.compute_net_margin_floor(100)
        assert result["ok"] is False
        assert "fee_data_invalid" in result["reason"]

    def test_non_usd_refuses(self):
        result = PricingEngine.compute_net_margin_floor(100, currency="EUR")
        assert result["ok"] is False
        assert "unsupported_currency" in result["reason"]

    def test_smart_price_invalid_cost_not_publishable(self):
        refused = PricingEngine.calculate_smart_price(None, 200)  # type: ignore[arg-type]
        assert refused["final_price"] is None
        assert refused["status"] in {"INVALID_COST", "INVALID_FLOOR"}
        assert refused["strategy"] == "REFUSED"

    def test_parse_money_rejects_bool_and_blank(self):
        assert parse_money(True) is None
        assert parse_money("") is None
        assert parse_money("  ") is None
        assert parse_money("47.13") == Decimal("47.13")


class TestPriceGuardRaiseAndPauseAd:
    def test_with_ads_below_ads_off_ok_flag_on_keeps_target(self):
        cost = 100.0
        floor_with = PricingEngine.net_margin_floor_price(cost)
        floor_no = PricingEngine.net_margin_floor_price(cost, ad_rate=0.0)
        target = round((floor_with + floor_no) / 2, 2)
        assert target < floor_with
        assert PricingEngine.net_margin_at_price(target, cost, ad_rate=0.0) >= Decimal("0.10")

        db = _seed_db(cost)
        with patch.object(
            repricing_guard, "_try_disable_ad", return_value=(True, "disabled")
        ) as mock_disable:
            decision = precheck_price(
                "SKU-100",
                target,
                db_path=db,
                execute_ad_pause=True,
            )
        mock_disable.assert_called_once()
        assert decision.ok is True
        assert decision.pause_ad is True
        assert decision.pause_ad_candidate is True
        assert decision.ad_paused is True
        assert decision.adjusted_price == pytest.approx(target)
        assert "pause_ad" in decision.as_report_fields()
        assert decision.as_report_fields()["pause_ad"] is True

    def test_with_ads_below_flag_off_uses_with_ad_floor(self):
        cost = 100.0
        floor_with = PricingEngine.net_margin_floor_price(cost)
        floor_no = PricingEngine.net_margin_floor_price(cost, ad_rate=0.0)
        target = round((floor_with + floor_no) / 2, 2)
        db = _seed_db(cost)
        with patch.object(
            repricing_guard, "_try_disable_ad", return_value=(True, "disabled")
        ) as mock_disable:
            decision = precheck_price(
                "SKU-100",
                target,
                db_path=db,
                execute_ad_pause=False,
            )
        mock_disable.assert_not_called()
        assert decision.ok is True
        assert decision.pause_ad is True
        assert decision.adjusted_price == pytest.approx(floor_with)
        assert decision.adjusted_price > target

    def test_ads_off_still_below_raises_instead_of_refuse(self):
        cost = 100.0
        floor_with = PricingEngine.net_margin_floor_price(cost)
        db = _seed_db(cost)
        with patch.object(
            repricing_guard, "_try_disable_ad", return_value=(True, "disabled")
        ) as mock_disable:
            decision = precheck_price(
                "SKU-100",
                100.0,
                db_path=db,
                execute_ad_pause=False,
            )
        mock_disable.assert_not_called()
        assert decision.ok is True
        assert "raised_to_net_floor" in decision.reason
        assert decision.adjusted_price == pytest.approx(floor_with)
        assert decision.pause_ad is True

    def test_unknown_cost_refuses(self):
        db = _seed_db()
        decision = precheck_price("SKU-MISSING", 150.0, db_path=db)
        assert decision.ok is False
        assert decision.reason == "unknown_or_invalid_cost"
        assert decision.adjusted_price is None

    def test_zero_and_negative_cost_refuse(self):
        for cost in (0, -3):
            decision = precheck_price(
                "SKU-X", 150.0, total_cost=cost, listing_id="L1"
            )
            assert decision.ok is False
            assert decision.reason == "unknown_or_invalid_cost"

    def test_string_price_and_cost(self):
        floor = PricingEngine.net_margin_floor_price("100")
        decision = precheck_price(
            "SKU-100",
            f"{floor:.2f}",
            total_cost="100",
            listing_id="L1",
        )
        assert decision.ok is True
        assert decision.reason == "ok"

    def test_int_sku_is_normalized(self):
        db = _seed_db(sku=12345, cost=100.0)
        floor = PricingEngine.net_margin_floor_price(100)
        decision = precheck_price(12345, floor, db_path=db)
        assert decision.ok is True
        assert decision.sku == "12345"

    def test_non_usd_refuses(self):
        decision = precheck_price(
            "SKU-100", 200.0, total_cost=100.0, listing_id="L1", currency="EUR"
        )
        assert decision.ok is False
        assert "unsupported_currency" in decision.reason

    def test_tuple_unpack_still_works(self):
        decision = precheck_price(
            "SKU-100", 200.0, total_cost=100.0, listing_id="L1"
        )
        ok, reason = decision
        assert ok is True
        assert reason == "ok"


class TestDryRunAndAdOptIn:
    def test_flag_off_never_calls_ad_api(self):
        db = _seed_db(100.0)
        mock_svc = MagicMock()
        mock_svc.find_ad_for_listing.return_value = {
            "campaign_id": "C1",
            "ad_id": "A1",
            "bid_percentage": 5.0,
        }
        with patch(
            "src.services.ebay_ad_service.EbayAdService", return_value=mock_svc
        ):
            decision = precheck_price(
                "SKU-100",
                150.0,
                db_path=db,
                execute_ad_pause=False,
            )
        mock_svc.find_ad_for_listing.assert_not_called()
        mock_svc.delete_ad.assert_not_called()
        assert decision.ad_paused is False
        assert decision.pause_ad is True

    def test_dry_run_never_calls_ad_api_even_if_flag_on(self):
        db = _seed_db(100.0)
        mock_svc = MagicMock()
        mock_svc.find_ad_for_listing.return_value = {
            "campaign_id": "C1",
            "ad_id": "A1",
            "bid_percentage": 5.0,
        }
        with patch(
            "src.services.ebay_ad_service.EbayAdService", return_value=mock_svc
        ):
            decision = precheck_price(
                "SKU-100",
                150.0,
                db_path=db,
                execute_ad_pause=True,
                dry_run=True,
            )
        mock_svc.find_ad_for_listing.assert_not_called()
        mock_svc.delete_ad.assert_not_called()
        assert decision.ad_paused is False
        assert decision.ok is True
        assert decision.adjusted_price == pytest.approx(
            PricingEngine.net_margin_floor_price(100.0)
        )

    def test_result_is_reportable(self):
        decision = PriceGuardResult(
            ok=True,
            reason="raised_to_net_floor",
            pause_ad=True,
            pause_ad_candidate=True,
            adjusted_price=156.38,
            sku="SKU-1",
        )
        row = decision.as_report_fields()
        assert row["pause_ad"] is True
        assert row["adjusted_price"] == 156.38


class TestSmartPriceUsesSameFloor:
    def test_anti_loss_matches_net_margin_floor(self):
        floor = PricingEngine.net_margin_floor_price(100)
        smart = PricingEngine.calculate_smart_price(100, 50)
        assert smart["final_price"] == pytest.approx(floor)
        assert smart["floor_price"] == pytest.approx(floor)
        assert smart["margin"] >= 0.10
        assert smart["pause_ad"] is False

    def test_competitive_stays_at_or_above_floor(self):
        floor = PricingEngine.net_margin_floor_price(100)
        smart = PricingEngine.calculate_smart_price(100, 400)
        assert smart["final_price"] + 1e-9 >= floor
        margin = PricingEngine.net_margin_at_price(smart["final_price"], 100)
        assert margin >= Decimal("0.10")

    def test_tiny_cost_still_clears_ten_percent(self):
        floor = PricingEngine.net_margin_floor_price("0.01")
        margin = PricingEngine.net_margin_at_price(floor, "0.01")
        assert margin >= Decimal("0.10")

    def test_none_sku_refuses_only_on_cost_not_on_name(self):
        decision = precheck_price(None, 200.0, total_cost=100.0, listing_id="L1")
        assert decision.ok is True
        assert decision.sku == ""
