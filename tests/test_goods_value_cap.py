"""Goods-value cap is product unit price only (shipping excluded)."""

from __future__ import annotations

import pytest

from src.utils.copy_skip_gate import (
    MAX_SUPPLIER_CARGO_COST_USD,
    REASON_CARGO_COST,
    REASON_PRODUCT_PRICE_MISSING,
    REASON_PRODUCT_PRICE_UNREADABLE,
    REASON_UNSUPPORTED_CURRENCY,
    evaluate_copy_skip,
    evaluate_goods_value_cap,
    parse_supplier_unit_price,
    safe_15_price,
    supplier_cargo_cost,
)
from tests.test_copy_skip_before_llm import CLEAR_DIMS, _decision


OWNER_ROWS = (
    ("W504P541843", 220, 60.53),
    ("W2709P566472", 135, 128.32),
    ("W708P588816", 219, 129.07),
    ("W2615P556780", 249, 46.70),
)


def test_cap_constant_is_250():
    assert MAX_SUPPLIER_CARGO_COST_USD == 250


@pytest.mark.parametrize(
    "raw, amount, error",
    [
        (None, None, REASON_PRODUCT_PRICE_MISSING),
        ("", None, REASON_PRODUCT_PRICE_MISSING),
        ("   ", None, REASON_PRODUCT_PRICE_MISSING),
        (0, None, REASON_PRODUCT_PRICE_MISSING),
        (-1, None, REASON_PRODUCT_PRICE_MISSING),
        (True, None, REASON_PRODUCT_PRICE_UNREADABLE),
        (False, None, REASON_PRODUCT_PRICE_UNREADABLE),
        (float("nan"), None, REASON_PRODUCT_PRICE_UNREADABLE),
        (float("inf"), None, REASON_PRODUCT_PRICE_UNREADABLE),
        ("not-a-price", None, REASON_PRODUCT_PRICE_UNREADABLE),
        ("USD", None, REASON_PRODUCT_PRICE_UNREADABLE),
        (250, 250.0, None),
        (250.00, 250.0, None),
        (250.01, 250.01, None),
        ("$250.00", 250.0, None),
        ("USD 250.01", 250.01, None),
        ("1,250.00", 1250.0, None),
        ("  249.0  ", 249.0, None),
        ("250.00\u00a0", 250.0, None),
    ],
)
def test_parse_supplier_unit_price_edges(raw, amount, error):
    parsed, _ccy, err = parse_supplier_unit_price(raw)
    assert parsed == amount
    assert err == error


def test_evaluate_goods_value_cap_boundaries():
    ok, detail = evaluate_goods_value_cap(250)
    assert ok == []
    assert detail["supplier_product_price_usd"] == 250.0
    ok_cents, _ = evaluate_goods_value_cap(250.00)
    assert ok_cents == []
    over, over_detail = evaluate_goods_value_cap(250.01)
    assert over == [REASON_CARGO_COST]
    assert over_detail["goods_value_cap_usd"] == 250
    over_str, _ = evaluate_goods_value_cap("$250.01")
    assert over_str == [REASON_CARGO_COST]


def test_evaluate_goods_value_cap_missing_unreadable_currency():
    missing, _ = evaluate_goods_value_cap(None)
    assert missing == [REASON_PRODUCT_PRICE_MISSING]
    blank, _ = evaluate_goods_value_cap("")
    assert blank == [REASON_PRODUCT_PRICE_MISSING]
    junk, _ = evaluate_goods_value_cap("abc")
    assert junk == [REASON_PRODUCT_PRICE_UNREADABLE]
    euro, detail = evaluate_goods_value_cap(80, currency="EUR")
    assert euro == [REASON_UNSUPPORTED_CURRENCY]
    assert "EUR" in str(detail.get("currency_reason") or "")
    yen, _ = evaluate_goods_value_cap("¥100")
    assert REASON_UNSUPPORTED_CURRENCY in yen
    specs_ccy, _ = evaluate_goods_value_cap(80, specs={"_supplier_currency": "GBP"})
    assert specs_ccy == [REASON_UNSUPPORTED_CURRENCY]
    usd_ok, _ = evaluate_goods_value_cap(80, currency="USD")
    assert usd_ok == []
    missing_ccy_ok, _ = evaluate_goods_value_cap(80)
    assert missing_ccy_ok == []


def test_evaluate_goods_value_cap_ranking_and_apply_false():
    both, _ = evaluate_goods_value_cap(None, currency="EUR")
    assert both[0] == REASON_PRODUCT_PRICE_MISSING
    assert REASON_UNSUPPORTED_CURRENCY in both
    skipped, detail = evaluate_goods_value_cap(999, currency="EUR", apply=False)
    assert skipped == []
    assert detail["supplier_product_price_usd"] == 999


def test_owner_skus_pass_when_shipping_pushes_landed_over_250():
    for sku, price, ship in OWNER_ROWS:
        cargo = supplier_cargo_cost(price, ship)
        assert cargo is not None and cargo > 250
        reasons, detail = evaluate_goods_value_cap(price)
        assert reasons == [], (sku, price, ship)
        decision = _decision(sku=sku, product_price=price, shipping=ship, include_market=False)
        assert REASON_CARGO_COST not in decision.reasons
        assert decision.detail["supplier_product_price_usd"] == price
        assert decision.detail["supplier_cargo_cost_usd"] == pytest.approx(round(cargo, 2))
        assert safe_15_price(price, ship) is not None


def test_copy_skip_and_safe_15_still_use_shipping_for_math():
    cargo = supplier_cargo_cost(220, 60.53)
    assert cargo == pytest.approx(280.53)
    with_ship = safe_15_price(220, 60.53)
    no_ship = safe_15_price(220, 0)
    assert with_ship is not None and no_ship is not None
    assert with_ship > no_ship
    decision = evaluate_copy_skip(
        sku="N710P100001",
        product_price=220,
        shipping=60.53,
        attributes=dict(CLEAR_DIMS),
        include_market=False,
    )
    assert REASON_CARGO_COST not in decision.reasons
    assert decision.detail["supplier_cargo_cost_usd"] == pytest.approx(280.53)


def test_zero_and_negative_product_price_are_missing_not_silent_pass():
    for value in (0, 0.0, -5, "0", "-1"):
        reasons, _ = evaluate_goods_value_cap(value)
        assert REASON_PRODUCT_PRICE_MISSING in reasons, value
        decision = _decision(product_price=value, shipping=10, include_market=False)
        assert REASON_PRODUCT_PRICE_MISSING in decision.reasons


def test_same_cap_helper_on_collect_analyze_publish_and_new_arrivals():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    copy_skip = (root / "src" / "utils" / "copy_skip_gate.py").read_text(encoding="utf-8")
    assert "evaluate_goods_value_cap(" in copy_skip
    assert "evaluate_copy_skip" in copy_skip
    publish = (root / "batch_publish.py").read_text(encoding="utf-8")
    assert "evaluate_goods_value_cap" in publish
    discover = (root / "src" / "plugins" / "terapeak_research" / "external_discovery.py").read_text(encoding="utf-8")
    assert discover.count("evaluate_goods_value_cap") >= 2
    server = (root / "server.py").read_text(encoding="utf-8")
    daily = (root / "daily_tasks.py").read_text(encoding="utf-8")
    analyze = (root / "batch_analyze.py").read_text(encoding="utf-8")
    assert "evaluate_copy_skip" in server and "evaluate_copy_skip" in daily and "evaluate_copy_skip" in analyze


def test_apply_cargo_false_skips_cap_for_ebay_listing_drafts():
    decision = evaluate_copy_skip(
        sku="N710P100001",
        product_price=999,
        shipping=10,
        attributes=dict(CLEAR_DIMS),
        include_market=False,
        apply_cargo=False,
    )
    assert REASON_CARGO_COST not in decision.reasons
    assert REASON_PRODUCT_PRICE_MISSING not in decision.reasons
