"""Tests for scripts/repair_broken_listings.build_repair title handling.

W3166P455683 (a chicken coop) sat on live eBay showing the raw Chinese GIGA
data dump. Every repair attempt bailed with ``title_rebuild_failed`` because
the source product name is literally "chicken coop" — 12 characters, under the
15-char floor for a rebuilt title. The title floor was abandoning the whole
repair, including the description rebuild that was the actual fix needed
(2026-07-27). A thin source title must degrade to "keep the current title",
never to "leave a broken description on live".
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def repair_mod():
    for name in ("dotenv",):
        if name not in sys.modules:
            sys.modules[name] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
    spec = importlib.util.spec_from_file_location(
        "repair_broken_listings", ROOT / "scripts" / "repair_broken_listings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, title TEXT, optimization TEXT)"
    )
    return connection


class _Snap:
    """Minimal stand-in for a source snapshot."""

    def __init__(self, title):
        self.title = title
        self.characteristics = [
            "Large Chicken Coop measures 123 in L x 26 in W x 44.5 in H for 4-8 chickens.",
            "Spacious play area with an elevated main house and nesting boxes for laying.",
            "Includes runways and ramps so poultry can move between levels safely.",
        ]
        self.attributes = {"Main Material": "Fir Wood"}
        self.specs = {}
        self.description_html = "<div>" + " ".join(self.characteristics) + "</div>"


def _seed(conn, sku, live_title):
    conn.execute(
        "INSERT INTO collected_products (sku, title, optimization) VALUES (?,?,?)",
        (sku, live_title, json.dumps({"aspects": {"Type": ["Chicken Coop"]}})),
    )
    conn.commit()


def _fake_dajian(monkeypatch, repair_mod, source_title):
    snap = _Snap(source_title)
    monkeypatch.setattr(repair_mod, "build_source_snapshot", lambda detail: snap)
    return types.SimpleNamespace(get_product_detail_by_sku=lambda sku: {"sku": sku})


class TestThinSourceTitleDoesNotAbandonRepair:
    def test_short_source_title_keeps_live_title_and_still_repairs(
        self, repair_mod, conn, monkeypatch
    ):
        _seed(conn, "W3166P455683", "chicken coop")
        dj = _fake_dajian(monkeypatch, repair_mod, "chicken coop")

        built, reason = repair_mod.build_repair(conn, dj, "W3166P455683")

        assert built is not None, f"repair abandoned: {reason}"
        new_title, new_desc, _aspects, _snap = built
        assert new_title == "chicken coop"  # kept, not invented
        assert "KEY FEATURES" in new_desc
        assert "chickens" in new_desc

    def test_longer_live_title_is_preferred_over_thin_source(
        self, repair_mod, conn, monkeypatch
    ):
        _seed(conn, "SKU-A", "Large Walk-In Chicken Coop with Nesting Boxes and Run")
        dj = _fake_dajian(monkeypatch, repair_mod, "coop")

        built, reason = repair_mod.build_repair(conn, dj, "SKU-A")

        assert built is not None, f"repair abandoned: {reason}"
        assert built[0] == "Large Walk-In Chicken Coop with Nesting Boxes and Run"

    def test_usable_source_title_still_wins(self, repair_mod, conn, monkeypatch):
        _seed(conn, "SKU-B", "old live title")
        dj = _fake_dajian(monkeypatch, repair_mod, "Large Walk-In Chicken Coop with Nesting Boxes")

        built, reason = repair_mod.build_repair(conn, dj, "SKU-B")

        assert built is not None, f"repair abandoned: {reason}"
        assert built[0] == "Large Walk-In Chicken Coop with Nesting Boxes"

    def test_both_titles_empty_still_reports_failure(self, repair_mod, conn, monkeypatch):
        _seed(conn, "SKU-C", "")
        dj = _fake_dajian(monkeypatch, repair_mod, "")

        built, reason = repair_mod.build_repair(conn, dj, "SKU-C")

        assert built is None
        assert reason == "title_rebuild_failed"
