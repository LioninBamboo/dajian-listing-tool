"""Trading reprice must send Motors-required product identifiers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_reprice_trading", ROOT / "scripts" / "audit_reprice_trading.py"
)
reprice = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = reprice
SPEC.loader.exec_module(reprice)


def test_revise_price_xml_includes_upc_brand_and_mpn():
    xml = reprice.build_revise_price_xml("188760790738", 56.63, token="TOK", brand="AquaRides")
    assert "<StartPrice>56.63</StartPrice>" in xml
    assert "<UPC>Does Not Apply</UPC>" in xml
    assert "<Name>Brand</Name>" in xml
    assert "<Value>AquaRides</Value>" in xml
    assert "<Name>Manufacturer Part Number</Name>" in xml
    assert "<Value>Does Not Apply</Value>" in xml
    assert "<ItemID>188760790738</ItemID>" in xml


def test_reprice_write_channel_splits_trading_store_and_inventory_canary():
    from src.services.repricing_guard import reprice_write_channel
    from types import SimpleNamespace

    trading = SimpleNamespace(listing_channel="trading")
    inventory = SimpleNamespace(listing_channel="inventory")
    assert reprice_write_channel("188760790738", trading) == "trading"
    assert reprice_write_channel("188683585596", trading) == "inventory"
    assert reprice_write_channel("188760790738", inventory) == "inventory"
