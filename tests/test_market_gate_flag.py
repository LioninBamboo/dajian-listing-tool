"""MARKET_GATE_ENABLED: far_above_market is advisory unless the flag is true."""

from __future__ import annotations

from src.utils.copy_skip_gate import (
    COPY_SKIP_STATUS,
    REASON_BLACKLIST_SELLER,
    REASON_CARGO_COST,
    REASON_FAR_ABOVE,
    REASON_MATERIAL_CONFLICT,
    evaluate_copy_skip,
    market_advisory_log_line,
    market_gate_enabled,
    safe_15_price,
)
from tests.test_copy_skip_before_llm import (
    CLEAR_DIMS,
    _Product,
    _decision,
    _run_analyze,
)


def _far_above_input():
    price, shipping = 80, 10
    safe = safe_15_price(price, shipping)
    assert safe is not None and safe > 0
    median = safe / 2.0
    return price, shipping, safe, median


def test_flag_false_far_above_is_advisory_only(monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "false")
    price, shipping, safe, median = _far_above_input()
    decision = _decision(product_price=price, shipping=shipping, market_median=median)
    assert decision.skip is False
    assert REASON_FAR_ABOVE not in decision.reasons
    assert decision.detail["far_above_market_advisory"] is True
    assert "market_median" in decision.detail
    assert decision.detail["market_median"] == median
    assert decision.detail["market_ratio"] == round(safe / median, 4)
    assert "ceiling" in decision.detail


def test_flag_true_far_above_still_skips(monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "true")
    price, shipping, _safe, median = _far_above_input()
    decision = _decision(product_price=price, shipping=shipping, market_median=median)
    assert decision.skip is True
    assert REASON_FAR_ABOVE in decision.reasons
    assert decision.detail.get("far_above_market_advisory") is not True


def test_missing_env_key_behaves_like_false(monkeypatch):
    monkeypatch.delenv("MARKET_GATE_ENABLED", raising=False)
    price, shipping, _safe, median = _far_above_input()
    decision = _decision(product_price=price, shipping=shipping, market_median=median)
    assert decision.skip is False
    assert REASON_FAR_ABOVE not in decision.reasons
    assert decision.detail["far_above_market_advisory"] is True
    assert "market_median" in decision.detail


def test_flag_false_other_gates_still_skip(monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "false")

    cargo = _decision(product_price=240, shipping=20, include_market=False)
    assert cargo.skip is True
    assert REASON_CARGO_COST in cargo.reasons
    assert cargo.detail["supplier_cargo_cost_usd"] == 260

    black = evaluate_copy_skip(
        sku="N735P000111",
        product_price=40,
        shipping=10,
        attributes=dict(CLEAR_DIMS),
        include_market=False,
    )
    assert black.skip is True
    assert REASON_BLACKLIST_SELLER in black.reasons

    conflict = _decision(
        title="Genuine Leather Sofa",
        description="A sofa.",
        attributes={**CLEAR_DIMS, "Main Material": "PU Leather"},
        include_market=False,
    )
    assert conflict.skip is True
    assert REASON_MATERIAL_CONFLICT in conflict.reasons


def test_market_gate_enabled_reads_env_each_call(monkeypatch):
    monkeypatch.delenv("MARKET_GATE_ENABLED", raising=False)
    assert market_gate_enabled() is False
    monkeypatch.setenv("MARKET_GATE_ENABLED", "YES")
    assert market_gate_enabled() is True
    monkeypatch.setenv("MARKET_GATE_ENABLED", "0")
    assert market_gate_enabled() is False
    monkeypatch.setenv("MARKET_GATE_ENABLED", "on")
    assert market_gate_enabled() is True


def test_market_advisory_log_line_matches_spec():
    assert market_advisory_log_line({}) is None
    assert market_advisory_log_line({"far_above_market_advisory": True, "market_ratio": 2.0}) == (
        "market advisory ratio=2.00 (gate off)"
    )


def test_flag_false_far_above_analyze_still_generates_copy(monkeypatch):
    monkeypatch.setenv("MARKET_GATE_ENABLED", "false")
    product = _Product(price=80, shipping=10)
    _price, _shipping, safe, median = _far_above_input()
    intel = {
        "pricing_basis": "BROWSE_CACHE",
        "price_stats": {"median": median, "avg": median},
        "total_listings": 12,
    }
    result, calls = _run_analyze(monkeypatch, product, intel=intel)
    assert calls["market"] == 1
    assert calls["optimize"] == 1
    assert product.status != COPY_SKIP_STATUS
    assert product.status == "READY"
    assert result["skipped"] == 0
    assert result["success"] == 1
    assert product.sku not in result.get("skip_reasons", {})
    assert safe > median * 1.15
