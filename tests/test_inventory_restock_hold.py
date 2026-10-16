from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from src.utils.inventory_restock_hold import (
    get_active_restock_hold,
    is_restock_held,
    should_block_positive_quantity,
)


def _write_holds(path: Path, holds: list[dict]) -> Path:
    path.write_text(json.dumps({"holds": holds}), encoding="utf-8")
    return path


def test_active_hold_blocks_restock_until_inclusive_until_date(tmp_path):
    until = date(2026, 8, 25)
    holds_path = _write_holds(
        tmp_path / "inventory_restock_holds.json",
        [{"sku": "W2500P479541", "until": until.isoformat(), "keep_quantity": 0}],
    )

    now = datetime(2026, 8, 20, 12, 0, 0)
    assert is_restock_held("W2500P479541", now=now, path=holds_path) is True
    assert should_block_positive_quantity("W2500P479541", 1, now=now, path=holds_path) is True
    assert should_block_positive_quantity("W2500P479541", 0, now=now, path=holds_path) is False

    hold = get_active_restock_hold("W2500P479541", now=now, path=holds_path)
    assert hold is not None
    assert hold["keep_quantity"] == 0
    assert hold["until"] == until

    on_until_date = datetime(2026, 8, 25, 23, 59, 0)
    assert is_restock_held("W2500P479541", now=on_until_date, path=holds_path) is True


def test_expired_or_missing_hold_does_not_block(tmp_path):
    yesterday = (date(2026, 8, 20) - timedelta(days=1)).isoformat()
    holds_path = _write_holds(
        tmp_path / "inventory_restock_holds.json",
        [{"sku": "W2500P479541", "until": yesterday}],
    )
    now = datetime(2026, 8, 20, 9, 0, 0)

    assert is_restock_held("W2500P479541", now=now, path=holds_path) is False
    assert is_restock_held("OTHER-SKU", now=now, path=holds_path) is False
    assert is_restock_held("W2500P479541", now=now, path=tmp_path / "missing.json") is False


def test_sync_keeps_held_sku_at_zero_instead_of_restocking(monkeypatch):
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    import src.plugins.inventory_sync.sync_service as sync_mod

    monkeypatch.setattr(sync_mod, "is_restock_held", lambda sku: sku == "W2500P479541")

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service.check_dajian_stock = lambda sku: (True, 120.0, 25.0, 7)
    service.get_last_sync_action = lambda sku: "out_of_stock"

    seen = {}

    def fake_update_ebay_quantity(sku, quantity):
        seen["sku"] = sku
        seen["quantity"] = quantity
        return True

    service.update_ebay_quantity = fake_update_ebay_quantity

    result = service._sync_single_product(
        {"sku": "W2500P479541", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )

    assert seen == {"sku": "W2500P479541", "quantity": 0}
    assert result.action == "restock_held"
    assert result.new_value == "库存保持为0"
    assert "保持为 0" in result.message


def test_sync_restocks_after_hold_expires(monkeypatch):
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    import src.plugins.inventory_sync.sync_service as sync_mod

    monkeypatch.setattr(sync_mod, "is_restock_held", lambda sku: False)

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)
    service.check_dajian_stock = lambda sku: (True, 120.0, 25.0, 7)
    service.get_last_sync_action = lambda sku: "restock_held"
    service._check_and_update_price = lambda *args, **kwargs: None

    seen = {}

    def fake_update_ebay_quantity(sku, quantity):
        seen["sku"] = sku
        seen["quantity"] = quantity
        return True

    service.update_ebay_quantity = fake_update_ebay_quantity

    result = service._sync_single_product(
        {"sku": "W2500P479541", "cost_breakdown": {}},
        dry_run=False,
        skip_ebay_check=True,
        favorites_set=set(),
    )

    assert seen == {"sku": "W2500P479541", "quantity": 1}
    assert result.action == "restocked"


def test_update_ebay_quantity_refuses_positive_qty_while_held(monkeypatch):
    from src.plugins.inventory_sync.sync_service import InventorySyncService
    import src.plugins.inventory_sync.sync_service as sync_mod

    monkeypatch.setattr(
        sync_mod,
        "should_block_positive_quantity",
        lambda sku, quantity: sku == "W2500P479541" and quantity > 0,
    )

    service = InventorySyncService.__new__(InventorySyncService)
    service.logger = logging.getLogger(__name__)

    assert service.update_ebay_quantity("W2500P479541", 1) is False


def test_sales_health_skips_ghost_restock_for_held_sku(monkeypatch):
    from scripts.sales_health_check import should_auto_restock_zero_listing
    import scripts.sales_health_check as shc

    monkeypatch.setattr(shc, "is_restock_held", lambda sku: sku == "W2500P479541")

    assert should_auto_restock_zero_listing("W2500P479541") is False
    assert should_auto_restock_zero_listing("OTHER-SKU") is True
