"""Inventory sync may write eBay prices only on material cost change or floor raise.

Regression for the price ping-pong between daily inventory sync (15% target)
and Mon/Thu smart reprice (live market price).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from src.plugins.inventory_sync.sync_service import (
    PRICE_WRITE_BASE_COST_KEY,
    SYNC_PRICE_CHANGE_MIN_USD,
    SYNC_PRICE_CHANGE_PCT,
    InventorySyncService,
    SyncResult,
    baseline_base_cost,
    decide_sync_price_action,
    is_material_base_cost_change,
    listing_price_below_cost_floor,
    parse_usd,
    resolve_current_listing_price,
)
from src.services.pricing_engine import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path, rows):
    """rows: iterable of (sku, cost_breakdown_dict, suggested_price)."""
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            status TEXT,
            listing_id TEXT,
            title TEXT,
            url TEXT,
            cost_breakdown TEXT,
            suggested_price REAL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO collected_products
        (sku, status, listing_id, title, url, cost_breakdown, suggested_price)
        VALUES (?, 'PUBLISHED', ?, '', '', ?, ?)
        """,
        [
            (str(sku), str(listing_id), json.dumps(breakdown), suggested)
            for sku, listing_id, breakdown, suggested in rows
        ],
    )
    conn.commit()
    conn.close()
    return db


def _row(db_path, sku):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT sku, cost_breakdown, suggested_price FROM collected_products WHERE sku = ?",
        (str(sku),),
    ).fetchone()
    conn.close()
    assert row is not None, f"missing sku {sku}"
    breakdown = json.loads(row["cost_breakdown"]) if row["cost_breakdown"] else {}
    return breakdown, row["suggested_price"]


def _service(tmp_path, rows, *, live_prices=None, verify_ok=True):
    db = _make_db(tmp_path, rows)
    service = InventorySyncService(db_path=str(db))
    service.logger = logging.getLogger("test_inventory_sync_price_guard")
    writes = []

    def fake_update(sku, price):
        writes.append((sku, price))
        return True

    service.update_ebay_price = fake_update
    service.verify_ebay_price = lambda sku, expected, tolerance=0.05: (
        (True, expected) if verify_ok else (False, expected)
    )
    live_prices = live_prices or {}
    service.get_live_offer_price = lambda sku: live_prices.get(str(sku), live_prices.get(sku))
    return service, writes, db


def _product(sku, breakdown, live_price=None):
    product = {"sku": sku, "cost_breakdown": dict(breakdown)}
    if live_price is not None:
        product["live_price"] = live_price
    return product


def _landed_and_floor(product_price, shipping_cost):
    landed = PricingEngine.calculate_dajian_cost(product_price, shipping_cost)["total_dajian_cost"]
    floor = PricingEngine.safe_floor_price(
        landed, float(PricingEngine.MIN_NET_MARGIN_ON_COST)
    )
    return landed, floor


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_named_threshold_constants_match_approved_defaults():
    assert SYNC_PRICE_CHANGE_PCT == 2.0
    assert SYNC_PRICE_CHANGE_MIN_USD == 1.0
    assert PRICE_WRITE_BASE_COST_KEY == "price_write_base_cost"


def test_parse_usd_accepts_strings_and_rejects_unknown():
    assert parse_usd("21.33") == pytest.approx(21.33)
    assert parse_usd(" 21.33 ") == pytest.approx(21.33)
    assert parse_usd(21.33) == pytest.approx(21.33)
    assert parse_usd(None) is None
    assert parse_usd("") is None
    assert parse_usd("   ") is None
    assert parse_usd("not-a-number") is None
    assert parse_usd(float("nan")) is None
    assert parse_usd(float("inf")) is None


def test_is_material_requires_both_thresholds_strictly_greater():
    # 2.5% but $0.80 → no
    assert is_material_base_cost_change(32.0, 32.80) is False
    # 1.5% but $3 → no
    assert is_material_base_cost_change(200.0, 203.0) is False
    # 3% and $2 → yes
    assert is_material_base_cost_change(66.67, 68.67) is True

    # same three for decreases
    assert is_material_base_cost_change(32.80, 32.0) is False
    assert is_material_base_cost_change(203.0, 200.0) is False
    assert is_material_base_cost_change(68.67, 66.67) is True

    # exactly 2% / exactly $1 → no (strictly greater)
    assert is_material_base_cost_change(50.0, 51.0) is False  # 2% and $1
    assert is_material_base_cost_change(50.0, 49.0) is False
    assert is_material_base_cost_change(100.0, 102.0) is False  # 2% and $2: pct not >
    assert is_material_base_cost_change(40.0, 40.80) is False  # 2% and $0.80


def test_is_material_unknown_costs_are_not_material():
    assert is_material_base_cost_change(None, 40.0) is False
    assert is_material_base_cost_change(0, 40.0) is False
    assert is_material_base_cost_change(40.0, None) is False
    assert is_material_base_cost_change("", 40.0) is False
    assert is_material_base_cost_change(40.0, "x") is False
    assert is_material_base_cost_change("21.33", "21.33") is False
    assert is_material_base_cost_change("40.00", "42.00") is True


def test_baseline_prefers_last_price_write_cost_not_last_sync_cost():
    assert baseline_base_cost({
        "base_cost": 41.20,
        PRICE_WRITE_BASE_COST_KEY: 40.0,
    }) == pytest.approx(40.0)
    assert baseline_base_cost({"base_cost": 40.0}) == pytest.approx(40.0)
    assert baseline_base_cost({}) is None
    assert baseline_base_cost(None) is None
    assert baseline_base_cost({"base_cost": 0}) is None
    assert baseline_base_cost({PRICE_WRITE_BASE_COST_KEY: "40.00"}) == pytest.approx(40.0)


def test_listing_price_below_floor_uses_existing_safe_floor():
    _landed, floor = _landed_and_floor(40.0, 5.0)
    assert listing_price_below_cost_floor(floor - 1.0, 40.0, 5.0) is True
    assert listing_price_below_cost_floor(floor, 40.0, 5.0) is False
    assert listing_price_below_cost_floor(floor + 1.0, 40.0, 5.0) is False
    assert listing_price_below_cost_floor(None, 40.0, 5.0) is False
    assert listing_price_below_cost_floor(10.0, None, 5.0) is False


def test_resolve_current_listing_price_ignores_stale_suggested_price():
    product = {
        "sku": "W465P194603",
        "suggested_price": 230.74,
        "cost_breakdown": {"selling_price": 202.46, "base_cost": 128.42},
    }
    assert resolve_current_listing_price(product, live_offer_price=None) == pytest.approx(202.46)

    no_reprice_field = {
        "sku": "W465P194603",
        "suggested_price": 230.74,
        "cost_breakdown": {"base_cost": 128.42},
    }
    assert resolve_current_listing_price(no_reprice_field, live_offer_price=202.46) == pytest.approx(202.46)
    assert resolve_current_listing_price(no_reprice_field, live_offer_price=None) is None


def test_decide_shipping_only_cents_move_is_not_a_write():
    decision = decide_sync_price_action(
        giga_product_price=107.09,
        giga_shipping_cost=21.21,
        cost_breakdown={"base_cost": 107.09 + 21.33, "selling_price": 202.46},
        live_listing_price=202.46,
    )
    assert decision.write is False
    assert decision.reason == "small_cost_change"


# ---------------------------------------------------------------------------
# Service: 11-02 example / live price / thresholds
# ---------------------------------------------------------------------------

def test_nov02_shipping_cents_move_does_not_write_ebay(tmp_path):
    sku = "W465P194603"
    old_shipping = 21.33
    new_shipping = 21.21
    product_price = 107.09
    live = 202.46
    stale_sync_target = 230.74
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {
            "base_cost": product_price + old_shipping,
            "product_price": product_price,
            "shipping_cost": old_shipping,
            "selling_price": live,
        }, stale_sync_target)],
        live_prices={sku: live},
    )

    result = service._check_and_update_price(
        sku,
        _product(sku, {
            "base_cost": product_price + old_shipping,
            "product_price": product_price,
            "shipping_cost": old_shipping,
            "selling_price": live,
        }),
        product_price,
        new_shipping,
        dry_run=False,
    )

    assert result is None
    assert writes == []
    breakdown, suggested = _row(db, sku)
    assert suggested == pytest.approx(stale_sync_target)
    assert breakdown["selling_price"] == pytest.approx(live)
    assert breakdown["base_cost"] == pytest.approx(product_price + new_shipping)
    assert breakdown[PRICE_WRITE_BASE_COST_KEY] == pytest.approx(product_price + old_shipping)


def test_w465_tiny_cost_move_keeps_live_reprice(tmp_path):
    sku = "W465P194603"
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 128.42, "selling_price": 202.46}, 230.74)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 128.42, "selling_price": 202.46}),
        107.21,
        21.21,
        dry_run=False,
    )
    assert result is None
    assert writes == []


@pytest.mark.parametrize(
    "old_base,new_base,expect_write",
    [
        (32.0, 32.80, False),   # +2.5% / $0.80
        (200.0, 203.0, False),  # +1.5% / $3
        (66.67, 68.67, True),   # +3% / $2
        (32.80, 32.0, False),   # -2.5% / $0.80
        (203.0, 200.0, False),  # -1.5% / $3
        (68.67, 66.67, True),   # -3% / $2
        (50.0, 51.0, False),    # exactly 2% and $1
        (50.0, 49.0, False),
    ],
)
def test_threshold_matrix_increase_and_decrease(tmp_path, old_base, new_base, expect_write):
    sku = "SKU-THRESHOLD"
    live = 400.0  # well above floor so only rule 1 can write
    product_price = new_base
    shipping = 0.0
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": old_base, "selling_price": live}, live)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": old_base, "selling_price": live}),
        product_price,
        shipping,
        dry_run=False,
    )
    if expect_write:
        assert result is not None
        assert writes and writes[0][0] == sku
    else:
        assert result is None
        assert writes == []


def test_env_overrides_thresholds(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNC_PRICE_CHANGE_PCT", "10")
    monkeypatch.setenv("SYNC_PRICE_CHANGE_MIN_USD", "5")
    sku = "SKU-ENV"
    live = 400.0
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 66.67, "selling_price": live}, live)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 66.67, "selling_price": live}),
        68.67,
        0.0,
        dry_run=False,
    )
    assert result is None
    assert writes == []


def test_live_price_below_floor_with_tiny_cost_change_raises_to_target(tmp_path):
    sku = "SKU-FLOOR"
    product_price = 40.0
    shipping = 5.0
    old_base = 45.06  # tiny drift vs 45.00
    _landed, floor = _landed_and_floor(product_price, shipping)
    live = round(floor - 8.0, 2)
    assert live < floor
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {
            "base_cost": old_base,
            "selling_price": live,
        }, 230.74)],
    )
    expected = service.calculate_new_price(product_price, shipping)
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": old_base, "selling_price": live}),
        product_price,
        shipping,
        dry_run=False,
    )
    assert result == pytest.approx(expected)
    assert writes == [(sku, expected)]
    breakdown, suggested = _row(db, sku)
    assert suggested == pytest.approx(expected)
    assert breakdown["selling_price"] == pytest.approx(expected)
    assert breakdown[PRICE_WRITE_BASE_COST_KEY] == pytest.approx(product_price + shipping)
    assert service.floor_raises == 1
    assert service.price_writes == 1


def test_floor_check_does_not_use_stale_suggested_price(tmp_path):
    sku = "SKU-STALE-DB"
    product_price = 40.0
    shipping = 5.0
    _landed, floor = _landed_and_floor(product_price, shipping)
    live = round(floor - 8.0, 2)
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 45.0}, 999.0)],  # suggested_price far above floor
        live_prices={sku: live},
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 45.0}),
        product_price,
        shipping,
        dry_run=False,
    )
    assert result is not None
    assert writes


def test_cumulative_drift_writes_once_when_threshold_crossed(tmp_path):
    """Baseline is last price-write cost, not last sync's refreshed base_cost.

    Five $0.30 moves on a $40 cost: 4 * $0.30 = $1.20 (3%) already crosses
    both strict thresholds, so the write happens on the 4th sync. A 5th move
    after the write starts a new baseline and is skipped.
    """
    sku = "SKU-DRIFT"
    live = 400.0
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": live}, live)],
    )

    write_steps = []
    for step, new_base in enumerate((40.30, 40.60, 40.90, 41.20, 41.50), start=1):
        breakdown, _ = _row(db, sku)
        result = service._check_and_update_price(
            sku,
            _product(sku, breakdown),
            new_base,
            0.0,
            dry_run=False,
        )
        if result is not None:
            write_steps.append(step)

    assert write_steps == [4]
    assert len(writes) == 1
    breakdown, _ = _row(db, sku)
    assert breakdown[PRICE_WRITE_BASE_COST_KEY] == pytest.approx(41.20)
    assert breakdown["base_cost"] == pytest.approx(41.50)


def test_cumulative_five_quarter_moves_on_40_writes_on_fifth(tmp_path):
    """$0.25 * 4 = $1.00 exactly → still no write; fifth $0.25 crosses."""
    sku = "SKU-DRIFT-25"
    live = 400.0
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": live}, live)],
    )
    write_steps = []
    for step, new_base in enumerate((40.25, 40.50, 40.75, 41.00, 41.25), start=1):
        breakdown, _ = _row(db, sku)
        result = service._check_and_update_price(
            sku,
            _product(sku, breakdown),
            new_base,
            0.0,
            dry_run=False,
        )
        if result is not None:
            write_steps.append(step)
    assert write_steps == [5]
    assert len(writes) == 1


@pytest.mark.parametrize(
    "old_base,new_product,new_shipping,live",
    [
        (0, 40.0, 5.0, 80.0),
        (None, 40.0, 5.0, 80.0),
        ({}, 40.0, 5.0, 80.0),  # missing handled via empty breakdown
        (40.0, None, 5.0, 80.0),
        ("21.33", "21.33", 0.0, 80.0),
        (40.0, 40.30, 0.0, None),
    ],
)
def test_unknown_inputs_do_not_crash_or_write(
    tmp_path, old_base, new_product, new_shipping, live
):
    sku = "SKU-UNKNOWN"
    if old_base == {}:
        breakdown = {}
    elif old_base is None:
        breakdown = {"selling_price": live} if live is not None else {}
    else:
        breakdown = {"base_cost": old_base}
        if live is not None:
            breakdown["selling_price"] = live
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", breakdown if isinstance(breakdown, dict) else {}, live or 0)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, breakdown if isinstance(breakdown, dict) else {}),
        new_product,
        new_shipping,
        dry_run=False,
    )
    if old_base == "21.33" and new_product == "21.33":
        assert result is None
        assert writes == []
        return
    assert result is None
    assert writes == []


def test_string_numbers_and_int_sku_are_safe(tmp_path):
    sku = 194603
    service, writes, db = _service(
        tmp_path,
        [("194603", "1", {"base_cost": "128.42", "selling_price": "202.46"}, 230.74)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": "128.42", "selling_price": "202.46"}),
        "107.21",
        "21.21",
        dry_run=False,
    )
    assert result is None
    assert writes == []
    breakdown, _ = _row(db, "194603")
    assert breakdown["base_cost"] == pytest.approx(128.42)


def test_qty_zero_oos_path_does_not_write_price(tmp_path):
    sku = "SKU-OOS"
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": 80.0}, 80.0)],
    )
    service.check_dajian_stock = lambda sku: (False, 40.0, 5.0, 0)
    qty_writes = []
    service.update_ebay_quantity = lambda sku, qty: qty_writes.append((sku, qty)) or True
    result = service._sync_single_product(
        _product(sku, {"base_cost": 40.0, "selling_price": 80.0}),
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )
    assert result.action == "out_of_stock"
    assert writes == []
    assert qty_writes == [(sku, 0)]


def test_dry_run_writes_nothing_even_when_material(tmp_path):
    sku = "SKU-DRY"
    live = 400.0
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 66.67, "selling_price": live}, live)],
    )
    before, suggested_before = _row(db, sku)
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 66.67, "selling_price": live}),
        68.67,
        0.0,
        dry_run=True,
    )
    assert result is not None  # would write
    assert writes == []
    after, suggested_after = _row(db, sku)
    assert after == before
    assert suggested_after == suggested_before


def test_dry_run_skip_does_not_refresh_cost(tmp_path):
    sku = "SKU-DRY-SKIP"
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": 80.0}, 80.0)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 40.0, "selling_price": 80.0}),
        40.30,
        0.0,
        dry_run=True,
    )
    assert result is None
    assert writes == []
    breakdown, _ = _row(db, sku)
    assert breakdown["base_cost"] == pytest.approx(40.0)
    assert PRICE_WRITE_BASE_COST_KEY not in breakdown


def test_sync_all_logs_price_summary_and_debug_skip(tmp_path, caplog):
    sku_skip = "SKU-SKIP"
    sku_write = "SKU-WRITE"
    live = 400.0
    service, writes, _db = _service(
        tmp_path,
        [
            (sku_skip, "1", {"base_cost": 40.0, "selling_price": live}, live),
            (sku_write, "2", {"base_cost": 66.67, "selling_price": live}, live),
        ],
    )
    service._load_favorites_set = lambda: set()
    service._prefetch_dajian_data = lambda skus: None

    def fake_stock(sku):
        if sku == sku_skip:
            return True, 40.30, 0.0, 2
        return True, 68.67, 0.0, 2

    service.check_dajian_stock = fake_stock
    service.get_last_sync_action = lambda sku: "no_change"

    from src.services.live_listing_coverage import LiveListing

    with caplog.at_level(logging.DEBUG, logger="test_inventory_sync_price_guard"):
        results = service.sync_all(
            dry_run=False,
            skip_ebay_check=True,
            skip_synced_today=False,
            live_listings=[
                LiveListing(item_id="1", sku=sku_skip),
                LiveListing(item_id="2", sku=sku_write),
            ],
            fetch_complete=True,
        )

    actions = {r.sku: r.action for r in results}
    assert actions[sku_skip] == "no_change"
    assert actions[sku_write] == "price_updated"
    assert service.price_writes == 1
    assert service.small_cost_change_skipped == 1
    assert service.floor_raises == 0
    summary = (
        f"price_writes={service.price_writes} "
        f"small_cost_change_skipped={service.small_cost_change_skipped} "
        f"floor_raises={service.floor_raises}"
    )
    assert any(summary in rec.getMessage() for rec in caplog.records)
    assert any("small cost change skipped" in rec.getMessage() for rec in caplog.records)


def test_restock_with_tiny_cost_change_does_not_write_price(tmp_path):
    sku = "SKU-RESTOCK"
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": 80.0}, 80.0)],
    )
    service.check_dajian_stock = lambda sku: (True, 40.30, 0.0, 3)
    service.get_last_sync_action = lambda sku: "out_of_stock"
    qty = []
    service.update_ebay_quantity = lambda sku, quantity: qty.append((sku, quantity)) or True
    result = service._sync_single_product(
        _product(sku, {"base_cost": 40.0, "selling_price": 80.0}),
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )
    assert result.action == "restocked"
    assert writes == []
    assert qty and qty[0][1] > 0


def test_verification_failure_does_not_update_db_or_count_write(tmp_path):
    sku = "SKU-VERIFY-FAIL"
    live = 400.0
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 66.67, "selling_price": live}, live)],
        verify_ok=False,
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        service._check_and_update_price(
            sku,
            _product(sku, {"base_cost": 66.67, "selling_price": live}),
            68.67,
            0.0,
            dry_run=False,
        )
    assert writes  # eBay write attempted
    breakdown, suggested = _row(db, sku)
    assert suggested == pytest.approx(live)
    assert breakdown.get(PRICE_WRITE_BASE_COST_KEY) is None
    assert service.price_writes == 0


def test_does_not_call_live_api_when_reprice_selling_price_present(tmp_path):
    sku = "SKU-NO-API"
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": 80.0}, 230.74)],
    )

    def boom(sku):
        raise AssertionError("live offer lookup must not run when selling_price is present")

    service.get_live_offer_price = boom
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 40.0, "selling_price": 80.0}),
        40.30,
        0.0,
        dry_run=False,
    )
    assert result is None
    assert writes == []


def test_nan_and_negative_costs_do_not_write(tmp_path):
    sku = "SKU-NAN"
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": 80.0}, 80.0)],
    )
    assert service._check_and_update_price(
        sku, _product(sku, {"base_cost": 40.0, "selling_price": 80.0}),
        float("nan"), 5.0, False,
    ) is None
    assert service._check_and_update_price(
        sku, _product(sku, {"base_cost": -10.0, "selling_price": 80.0}),
        40.0, 5.0, False,
    ) is None
    assert writes == []


def test_decision_helpers_do_not_branch_on_store_profile():
    """GP/Aqua ops profiles share this module; the rule is store-agnostic."""
    import inspect
    source = inspect.getsource(decide_sync_price_action)
    assert "store_profile" not in source
    assert "get_store_profile" not in source


def test_missing_old_cost_still_raises_when_live_below_floor(tmp_path):
    sku = "SKU-NO-BASELINE"
    product_price = 40.0
    shipping = 5.0
    _landed, floor = _landed_and_floor(product_price, shipping)
    live = round(floor - 8.0, 2)
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"selling_price": live}, 230.74)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"selling_price": live}),
        product_price,
        shipping,
        dry_run=False,
    )
    assert result is not None
    assert writes


def test_stale_suggested_below_floor_is_ignored_when_live_is_safe(tmp_path):
    sku = "SKU-SUGGESTED-TRAP"
    product_price = 40.0
    shipping = 5.0
    _landed, floor = _landed_and_floor(product_price, shipping)
    live = round(floor + 20.0, 2)
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 45.0, "selling_price": live}, 1.0)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 45.0, "selling_price": live}),
        product_price,
        shipping,
        dry_run=False,
    )
    assert result is None
    assert writes == []


def test_material_write_is_not_counted_as_floor_raise(tmp_path):
    sku = "SKU-BOTH"
    _landed, floor = _landed_and_floor(66.67, 0.0)
    live = round(floor - 8.0, 2)
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 66.67, "selling_price": live}, live)],
    )
    result = service._check_and_update_price(
        sku,
        _product(sku, {"base_cost": 66.67, "selling_price": live}),
        68.67,
        0.0,
        dry_run=False,
    )
    assert result is not None
    assert writes
    assert service.price_writes == 1
    assert service.floor_raises == 0


def test_zero_and_blank_giga_price_do_not_write(tmp_path):
    sku = "SKU-ZERO"
    service, writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": 80.0}, 80.0)],
    )
    assert service._check_and_update_price(
        sku, _product(sku, {"base_cost": 40.0, "selling_price": 80.0}),
        0, 5.0, False,
    ) is None
    assert service._check_and_update_price(
        sku, _product(sku, {"base_cost": 40.0, "selling_price": 80.0}),
        "", 5.0, False,
    ) is None
    assert writes == []


def test_json_string_cost_breakdown_and_whitespace_shipping(tmp_path):
    sku = "SKU-JSON"
    live = 400.0
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": live}, live)],
    )
    product = {
        "sku": sku,
        "cost_breakdown": json.dumps({"base_cost": 40.0, "selling_price": live}),
    }
    result = service._check_and_update_price(
        sku, product, "40.30", "  0.00  ", False,
    )
    assert result is None
    assert writes == []
    breakdown, _ = _row(db, sku)
    assert breakdown["base_cost"] == pytest.approx(40.30)
    assert breakdown[PRICE_WRITE_BASE_COST_KEY] == pytest.approx(40.0)


def test_skip_persists_live_offer_price_so_next_sync_skips_api(tmp_path):
    sku = "SKU-PERSIST-LIVE"
    service, writes, db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0}, 230.74)],
        live_prices={sku: 80.0},
    )
    calls = {"n": 0}
    real = service.get_live_offer_price

    def counted(sku):
        calls["n"] += 1
        return real(sku)

    service.get_live_offer_price = counted
    first = service._check_and_update_price(
        sku, _product(sku, {"base_cost": 40.0}), 40.30, 0.0, False,
    )
    assert first is None
    assert calls["n"] == 1
    breakdown, suggested = _row(db, sku)
    assert breakdown["selling_price"] == pytest.approx(80.0)
    assert suggested == pytest.approx(230.74)

    def boom(sku):
        raise AssertionError("second skip must use stored selling_price")

    service.get_live_offer_price = boom
    second = service._check_and_update_price(
        sku, _product(sku, breakdown), 40.60, 0.0, False,
    )
    assert second is None
    assert writes == []


def test_skip_does_not_log_credentials(tmp_path, caplog):
    sku = "SKU-LOG"
    service, _writes, _db = _service(
        tmp_path,
        [(sku, "1", {"base_cost": 40.0, "selling_price": 80.0}, 80.0)],
    )
    with caplog.at_level(logging.DEBUG, logger="test_inventory_sync_price_guard"):
        service._check_and_update_price(
            sku,
            _product(sku, {"base_cost": 40.0, "selling_price": 80.0}),
            40.30,
            0.0,
            dry_run=False,
        )
    blob = " ".join(rec.getMessage() for rec in caplog.records).lower()
    assert "bearer" not in blob
    assert "token" not in blob
    assert "secret" not in blob
