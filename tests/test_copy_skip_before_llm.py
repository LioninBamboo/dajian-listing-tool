"""Unlistable SKUs must skip LLM listing copy before any token spend."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.services.conversion_diagnoser import FLOOR_OVER_MARKET_LOCK, PRICE_OVERPRICED
from src.services.pricing_engine import PricingEngine
from src.utils.copy_skip_gate import (
    COPY_SKIP_STATUS,
    MAX_SUPPLIER_CARGO_COST_USD,
    NEAR_MARKET_BAND,
    REASON_BLACKLIST_SELLER,
    REASON_CARGO_COST,
    REASON_FAR_ABOVE,
    REASON_MATERIAL_CONFLICT,
    REASON_UNCLEAR_DIMS,
    evaluate_copy_skip,
    market_median_from_intel,
    safe_15_price,
    supplier_cargo_cost,
)
from src.utils.listing_fact_sheet import extract_fact_sheet

ROOT = Path(__file__).resolve().parents[1]

CLEAR_DIMS = {
    "Assembled Length (in.)": "40",
    "Assembled Width (in.)": "20",
    "Assembled Height (in.)": "18",
}


def _decision(**overrides):
    payload = dict(
        sku="N710P100001",
        product_price=40,
        shipping=10,
        attributes=dict(CLEAR_DIMS),
        specs={},
        description="Oak side table.",
        title="Oak Side Table",
        include_market=True,
        dims_mode="require",
    )
    payload.update(overrides)
    return evaluate_copy_skip(**payload)


def test_near_market_band_matches_dryrun_overpriced_band():
    assert NEAR_MARKET_BAND == FLOOR_OVER_MARKET_LOCK == PRICE_OVERPRICED == 1.15
    assert float(PricingEngine.MIN_NET_MARGIN_ON_COST) == 0.10
    assert MAX_SUPPLIER_CARGO_COST_USD == 200


def test_blacklist_cargo_dims_and_material_reasons():
    black = _decision(sku="n735P999", include_market=False)
    assert black.reasons == [REASON_BLACKLIST_SELLER]

    cargo = _decision(product_price=180, shipping=25, include_market=False)
    assert REASON_CARGO_COST in cargo.reasons
    assert supplier_cargo_cost(180, 25) == 205

    missing = _decision(attributes={}, specs={}, description="", include_market=False)
    assert missing.reasons == [REASON_UNCLEAR_DIMS]

    placeholder = _decision(
        attributes={
            "Assembled Length (in.)": "See Description",
            "Assembled Width (in.)": "N/A",
            "Assembled Height (in.)": "Refer to Product Images",
        },
        description="",
        include_market=False,
        dims_mode="defer_missing",
    )
    assert placeholder.reasons == [REASON_UNCLEAR_DIMS]

    # Collect intake: absent dims wait for supplier enrichment.
    deferred = _decision(
        attributes={},
        specs={"Package Length (in.)": "40", "Package Width (in.)": "20", "Package Height (in.)": "18"},
        description="",
        include_market=False,
        dims_mode="defer_missing",
    )
    assert REASON_UNCLEAR_DIMS not in deferred.reasons

    # Analyze must not treat package dims as item dims.
    package_only = _decision(
        attributes={},
        specs={"Package Length (in.)": "40", "Package Width (in.)": "20", "Package Height (in.)": "18"},
        description="",
        include_market=False,
        dims_mode="require",
    )
    assert package_only.reasons == [REASON_UNCLEAR_DIMS]

    conflict = _decision(
        title="Genuine Leather Sofa",
        description="A sofa.",
        attributes={**CLEAR_DIMS, "Main Material": "PU Leather"},
        include_market=False,
    )
    assert conflict.reasons == [REASON_MATERIAL_CONFLICT]
    assert "genuine leather" in conflict.detail["material_conflicts"]

    canvas = _decision(
        title="Canvas Bell Tent",
        description="A tent.",
        attributes={**CLEAR_DIMS, "Main Material": "Rubberwood"},
        include_market=False,
    )
    assert REASON_MATERIAL_CONFLICT in canvas.reasons
    assert "canvas" in canvas.detail["material_conflicts"]

    grounded = _decision(
        title="Genuine Leather Sofa",
        description="Upholstered in genuine leather over a PU leather base.",
        attributes={**CLEAR_DIMS, "Main Material": "PU Leather"},
        include_market=False,
    )
    assert REASON_MATERIAL_CONFLICT not in grounded.reasons


def test_aluminium_synonym_and_generic_wood_are_not_material_conflicts():
    aluminium = _decision(
        title="Aluminium Patio Chair",
        description="Outdoor chair.",
        attributes={**CLEAR_DIMS, "Main Material": "Aluminum"},
        include_market=False,
    )
    assert REASON_MATERIAL_CONFLICT not in aluminium.reasons

    wood = _decision(
        title="Wood Coffee Table",
        description="Coffee table.",
        attributes={**CLEAR_DIMS, "Main Material": "MDF"},
        include_market=False,
    )
    assert REASON_MATERIAL_CONFLICT not in wood.reasons


def test_safe_15_no_market_is_not_a_copy_skip():
    decision = _decision(market_median=None, include_market=True)
    assert decision.skip is False
    assert decision.detail["pricing_basis"] == "SAFE_15_NO_MARKET"
    assert market_median_from_intel({"pricing_basis": "SAFE_15_NO_MARKET", "price_stats": {}}) is None


def test_far_above_when_safe_price_exceeds_near_market_band():
    price, shipping = 80, 10
    safe = safe_15_price(price, shipping)
    assert safe is not None and safe > 0
    median = safe / NEAR_MARKET_BAND - 1
    above = _decision(product_price=price, shipping=shipping, market_median=median)
    assert REASON_FAR_ABOVE in above.reasons
    assert above.detail["safe_15_price"] > above.detail["near_market_ceiling"]

    # Exactly on the band is still inside it.
    ceiling_median = safe / NEAR_MARKET_BAND
    edge = _decision(product_price=price, shipping=shipping, market_median=ceiling_median)
    assert REASON_FAR_ABOVE not in edge.reasons

    near = _decision(product_price=price, shipping=shipping, market_median=safe * 2)
    assert near.skip is False


def test_ebay_listing_price_is_not_supplier_cargo_without_a_live_quote():
    decision = _decision(
        product_price=400,
        shipping=0,
        specs={"_price_basis": "ebay_listing"},
        apply_cargo=False,
        include_market=False,
    )
    assert REASON_CARGO_COST not in decision.reasons


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _call_lines(func: ast.AST, callee: str) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        func_node = node.func
        name = ""
        if isinstance(func_node, ast.Name):
            name = func_node.id
        elif isinstance(func_node, ast.Attribute):
            name = func_node.attr
        if name == callee:
            lines.append(node.lineno)
    return lines


def test_analyze_callers_gate_before_llm_copy():
    targets = (
        ("server.py", "analyze_product_task"),
        ("daily_tasks.py", "analyze_collected_products"),
        ("batch_analyze.py", "main"),
    )
    for relative, func_name in targets:
        func = _function(ROOT / relative, func_name)
        gate_lines = _call_lines(func, "evaluate_copy_skip")
        copy_lines = _call_lines(func, "optimize_product_full_with_timeout")
        assert gate_lines, relative
        assert copy_lines, relative
        assert min(gate_lines) < min(copy_lines)

    analyze_src = ast.get_source_segment(
        (ROOT / "server.py").read_text(encoding="utf-8-sig"),
        _function(ROOT / "server.py", "analyze_product_task"),
    )
    assert analyze_src
    assert "attrs[a_key] = str(specs[p_key])" not in analyze_src

    collect = _function(ROOT / "server.py", "collect_product")
    gate_lines = _call_lines(collect, "evaluate_copy_skip")
    schedule_lines = [
        node.lineno
        for node in ast.walk(collect)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_task"
    ]
    assert gate_lines and schedule_lines
    assert min(gate_lines) < min(schedule_lines)


class _Product:
    def __init__(self, **kwargs):
        self.sku = kwargs.get("sku", "N710P100001")
        self.status = "COLLECTED"
        self.title = kwargs.get("title", "Oak Side Table")
        self.description = kwargs.get("description", "Solid oak side table.")
        self.attributes = kwargs.get("attributes", dict(CLEAR_DIMS))
        self.specs = kwargs.get("specs", {})
        self.price = kwargs.get("price", 40)
        self.shipping = kwargs.get("shipping", 10)
        self.images = ["http://example.com/a.jpg", "http://example.com/b.jpg"]
        self.videos = []
        self.logs = []
        self.optimization = None
        self.cost_breakdown = None


class _Query:
    def __init__(self, product):
        self._product = product

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return [self._product]


class _Session:
    def __init__(self, product):
        self._product = product

    def query(self, *args, **kwargs):
        return _Query(self._product)

    def merge(self, product):
        return product

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _run_analyze(monkeypatch, product, *, intel, live_quote=None):
    import daily_tasks

    calls = {"optimize": 0, "fact_sheet": 0, "market": 0, "live": 0}

    def _optimize(*args, **kwargs):
        calls["optimize"] += 1
        return {
            "title": "Oak Side Table 40 in",
            "description": "<p>Oak side table.</p>",
            "aspects": {"Brand": ["Test"]},
            "categoryId": "38204",
        }

    def _fact_sheet(*args, **kwargs):
        calls["fact_sheet"] += 1
        raise AssertionError("FactSheet LLM invent must not run for this path")

    class _Qwen:
        def fetch_market_intelligence(self, title, category_id=None):
            calls["market"] += 1
            return intel

    def _live(sku):
        calls["live"] += 1
        return live_quote

    monkeypatch.setattr("src.db.collection_db.SessionLocal", lambda: _Session(product))
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *args, **kwargs: None)
    monkeypatch.setattr("qwen_optimizer.optimize_product_full_with_timeout", _optimize)
    monkeypatch.setattr("src.utils.listing_fact_sheet.extract_fact_sheet", _fact_sheet)
    monkeypatch.setattr("qwen_optimizer.QwenOptimizer", lambda api_key: _Qwen())
    monkeypatch.setattr("src.utils.copy_skip_gate.lookup_live_supplier_quote", _live)
    monkeypatch.setenv("QWEN_API_KEY", "dummy")
    monkeypatch.setattr(daily_tasks, "_open_listing_qc_connection", lambda: None)

    def _qc(**kwargs):
        return {
            "status": "pass",
            "blockers": [],
            "violations": [],
            "source_fingerprint": "s",
            "candidate_fingerprint": "c",
        }

    monkeypatch.setattr("src.services.listing_qc.run_listing_qc", _qc)
    monkeypatch.setattr(
        "src.utils.listing_quality_gate.normalize_generated_listing",
        lambda opt_data, **kwargs: opt_data,
    )
    result = daily_tasks.analyze_collected_products(sku_filter=[product.sku])
    return result, calls


@pytest.mark.parametrize(
    ("sku", "price", "shipping", "title", "attributes", "intel", "live", "reason", "market_called"),
    [
        ("N735P000111", 40, 10, "Oak Side Table", CLEAR_DIMS, None, None, REASON_BLACKLIST_SELLER, False),
        ("N710P100001", 180, 30, "Oak Side Table", CLEAR_DIMS, None, None, REASON_CARGO_COST, False),
        ("N710P100001", 40, 10, "Oak Side Table", {}, None, None, REASON_UNCLEAR_DIMS, False),
        (
            "N710P100001",
            40,
            10,
            "Genuine Leather Sofa",
            {**CLEAR_DIMS, "Main Material": "PU Leather"},
            None,
            None,
            REASON_MATERIAL_CONFLICT,
            False,
        ),
    ],
)
def test_unlistable_analyze_never_calls_llm_copy(
    monkeypatch, sku, price, shipping, title, attributes, intel, live, reason, market_called
):
    product = _Product(sku=sku, price=price, shipping=shipping, title=title, attributes=attributes, description="A sofa.")
    result, calls = _run_analyze(monkeypatch, product, intel=intel, live_quote=live)
    assert calls["optimize"] == 0
    assert calls["fact_sheet"] == 0
    assert calls["market"] == (1 if market_called else 0)
    assert product.status == COPY_SKIP_STATUS
    assert reason in product.cost_breakdown["copy_skip_reasons"]
    assert any(line.startswith(f"copy_skip:{reason}") for line in product.logs)
    assert product.optimization is None
    assert result["skipped"] == 1
    assert result["success"] == 0
    assert extract_fact_sheet  # imported helper stays unused on this path


def test_live_cargo_recheck_skips_before_llm(monkeypatch):
    product = _Product(price=40, shipping=10)
    result, calls = _run_analyze(
        monkeypatch,
        product,
        intel=None,
        live_quote={"price": 190, "shipping": 20},
    )
    assert calls["optimize"] == 0
    assert calls["fact_sheet"] == 0
    assert calls["live"] == 1
    assert calls["market"] == 0
    assert product.price == 190
    assert product.shipping == 20
    assert REASON_CARGO_COST in result["skip_reasons"][product.sku]


def test_far_above_skips_after_market_fetch_and_before_copy(monkeypatch):
    product = _Product(price=80, shipping=10)
    safe = safe_15_price(80, 10)
    median = safe / NEAR_MARKET_BAND - 1
    intel = {
        "pricing_basis": "BROWSE_CACHE",
        "price_stats": {"median": median, "avg": median},
        "total_listings": 12,
    }
    result, calls = _run_analyze(monkeypatch, product, intel=intel)
    assert calls["market"] == 1
    assert calls["optimize"] == 0
    assert calls["fact_sheet"] == 0
    assert result["skip_reasons"][product.sku] == [REASON_FAR_ABOVE]
    assert product.status == COPY_SKIP_STATUS


def test_safe_15_no_market_and_near_market_still_generate_copy(monkeypatch):
    no_market = _Product(sku="N710P200001")
    result, calls = _run_analyze(
        monkeypatch,
        no_market,
        intel={"pricing_basis": "SAFE_15_NO_MARKET", "price_stats": {}, "total_listings": 0},
    )
    assert calls["optimize"] == 1
    assert calls["fact_sheet"] == 0
    assert result["success"] == 1
    assert no_market.status == "READY"
    assert no_market.optimization["title"]

    near = _Product(sku="N710P200002", price=80, shipping=10)
    safe = safe_15_price(80, 10)
    result, calls = _run_analyze(
        monkeypatch,
        near,
        intel={
            "pricing_basis": "BROWSE",
            "price_stats": {"median": safe * 2, "avg": safe * 2},
            "total_listings": 8,
        },
    )
    assert calls["optimize"] == 1
    assert near.status == "READY"
    assert result["skipped"] == 0
