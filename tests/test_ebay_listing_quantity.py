from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients.dajian_client import extract_available_inventory_quantity
from src.utils.ebay_quantity import (
    SupplierOutOfStockError,
    assess_quantity_alignment,
    coerce_nonnegative_quantity,
    determine_target_ebay_quantity,
    normalize_ebay_listing_quantity,
    resolve_publish_quantity,
)


def test_extract_available_inventory_quantity_prefers_buyer_inventory():
    inventory = {
        "buyerInventoryInfo": {"totalBuyerAvailableInventory": 7},
        "sellerInventoryInfo": {"sellerAvailableInventory": 19},
    }

    assert extract_available_inventory_quantity(inventory) == 7


def test_extract_available_inventory_quantity_falls_back_to_seller_inventory():
    inventory = {
        "buyerInventoryInfo": {"totalBuyerAvailableInventory": 0},
        "sellerInventoryInfo": {"sellerAvailableInventory": 12},
    }

    assert extract_available_inventory_quantity(inventory) == 12


def test_normalize_ebay_listing_quantity_caps_and_keeps_positive_default(monkeypatch):
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")

    assert normalize_ebay_listing_quantity(18) == 1
    assert normalize_ebay_listing_quantity(0) == 1
    assert normalize_ebay_listing_quantity("bad", fallback=3) == 1


def test_coerce_nonnegative_quantity_allows_zero_and_invalid_none():
    assert coerce_nonnegative_quantity("7") == 7
    assert coerce_nonnegative_quantity(0) == 0
    assert coerce_nonnegative_quantity(-4) == 0
    assert coerce_nonnegative_quantity("bad") is None


def test_determine_target_ebay_quantity_returns_zero_for_supplier_oos(monkeypatch):
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")

    assert determine_target_ebay_quantity(0) == 0
    assert determine_target_ebay_quantity(25) == 1


def test_assess_quantity_alignment_treats_live_qty_one_as_aligned(monkeypatch):
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")

    decision = assess_quantity_alignment(live_quantity=1, supplier_quantity=47)

    assert decision.status == "aligned"
    assert decision.desired_quantity == 1
    assert decision.should_update is False


def test_assess_quantity_alignment_flags_live_qty_above_one(monkeypatch):
    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")

    decision = assess_quantity_alignment(live_quantity=6, supplier_quantity=47)

    assert decision.status == "quantity_mismatch"
    assert decision.desired_quantity == 1
    assert decision.should_update is True


def test_assess_quantity_alignment_flags_supplier_out_of_stock():
    decision = assess_quantity_alignment(live_quantity=6, supplier_quantity=0)

    assert decision.status == "supplier_out_of_stock"
    assert decision.desired_quantity == 0
    assert decision.should_update is True


def test_assess_quantity_alignment_handles_unknown_supplier_quantity():
    decision = assess_quantity_alignment(live_quantity=6, supplier_quantity=None)

    assert decision.status == "supplier_unknown"
    assert decision.desired_quantity is None
    assert decision.should_update is False


def test_resolve_publish_quantity_uses_live_supplier_quantity_with_cap(monkeypatch):
    class FakeDajianClient:
        def get_stock_info(self, sku):
            assert sku == "SKU-LIVE"
            return {"quantity": 23}

    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")

    quantity = resolve_publish_quantity(
        "SKU-LIVE",
        dajian_client=FakeDajianClient(),
        logger=logging.getLogger(__name__),
    )

    assert quantity == 1


def test_resolve_publish_quantity_falls_back_when_supplier_lookup_fails(monkeypatch):
    class FakeDajianClient:
        def get_stock_info(self, sku):
            raise RuntimeError("boom")

    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")

    quantity = resolve_publish_quantity(
        "SKU-FALLBACK",
        fallback=4,
        lookup_failure_fallback=1,
        dajian_client=FakeDajianClient(),
        logger=logging.getLogger(__name__),
    )

    assert quantity == 1


def test_resolve_publish_quantity_defaults_failure_fallback_to_listing_cap(monkeypatch):
    class FakeDajianClient:
        def get_stock_info(self, sku):
            raise RuntimeError("boom")

    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")

    quantity = resolve_publish_quantity(
        "SKU-FALLBACK-DEFAULT",
        dajian_client=FakeDajianClient(),
        logger=logging.getLogger(__name__),
    )

    assert quantity == 1


def test_resolve_publish_quantity_raises_when_supplier_has_zero_stock(monkeypatch):
    class FakeDajianClient:
        def get_stock_info(self, sku):
            return {"quantity": 0}

    monkeypatch.setenv("EBAY_LISTING_QUANTITY_CAP", "1")

    try:
        resolve_publish_quantity(
            "SKU-OOS",
            dajian_client=FakeDajianClient(),
            logger=logging.getLogger(__name__),
        )
    except SupplierOutOfStockError as exc:
        assert "SKU-OOS" in str(exc)
    else:
        raise AssertionError("expected SupplierOutOfStockError")
