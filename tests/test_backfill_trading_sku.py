"""SKU backfill must send Motors identifiers and classify Inventory listings."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "backfill_trading_sku", ROOT / "scripts" / "backfill_trading_sku.py"
)
sku_mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sku_mod
SPEC.loader.exec_module(sku_mod)


def test_inventory_canary_is_classified_as_inventory_channel():
    assert sku_mod.sku_backfill_channel("188683585596", "W2913P449891") == "inventory"
    assert sku_mod.sku_backfill_channel("188752675328", "W3611P453305") == "trading"
