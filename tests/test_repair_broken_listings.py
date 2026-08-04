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
import re
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


def test_source_title_trailing_connector_is_trimmed_after_word_safe_cut(
    repair_mod, conn, monkeypatch
):
    source_title = (
        "Twin XL over Twin XL Metal Bunk Bed/Metal Loft Bed,Separable Bunk Beds,with "
        "raised security fence,Walnut Color"
    )
    _seed(conn, "W1580S00647", "Twin XL Metal Bunk Bed Frame with Ladder Walnut")
    dj = _fake_dajian(monkeypatch, repair_mod, source_title)

    built, reason = repair_mod.build_repair(conn, dj, "W1580S00647")

    assert built is not None, reason
    assert not re.search(r"\b(?:with|for|and|a|of|&)\s*$", built[0], re.IGNORECASE)


def test_repair_description_uses_source_dimensions_from_existing_aspects(
    repair_mod, conn, monkeypatch
):
    """A missing source width must not drop the trusted three-axis dimensions."""
    sku = "W1586135449"
    conn.execute(
        "INSERT INTO collected_products (sku, title, optimization) VALUES (?,?,?)",
        (
            sku,
            "4 Pack Rustproof Metal Garden Trellis 71 in x 20 in for Climbing Plants",
            json.dumps(
                {
                    "aspects": {
                        "Item Length": ["79.5 in"],
                        "Item Width": ["19.7 in"],
                        "Item Height": ["71.0 in"],
                    }
                }
            ),
        ),
    )
    conn.commit()

    class DimensionSnap:
        title = "4 Pack Rustproof Metal Garden Trellis 71 in x 20 in for Climbing Plants"
        characteristics = ["Four 19.7 inch wide x 71 inch high trellises."]
        attributes = {
            "Assembled Length (in.)": "79.50",
            "Assembled Height (in.)": "71.00",
            "Main Material": "Iron",
        }
        specs = {}
        description_html = "<div>Four trellises</div>"

    monkeypatch.setattr(repair_mod, "build_source_snapshot", lambda detail: DimensionSnap())
    dj = types.SimpleNamespace(get_product_detail_by_sku=lambda requested_sku: {"sku": requested_sku})

    built, reason = repair_mod.build_repair(conn, dj, sku)

    assert built is not None, reason
    assert "79.5" in built[1]
    assert "19.7" in built[1]
    assert "71" in built[1]


def test_repair_merges_missing_fresh_source_dimensions_from_stored_snapshot(
    repair_mod, conn, monkeypatch
):
    """A sparse refresh must not discard trusted source measurements."""
    conn.execute("ALTER TABLE collected_products ADD COLUMN attributes TEXT")
    sku = "W2564P00135"
    conn.execute(
        "INSERT INTO collected_products (sku, title, optimization, attributes) VALUES (?,?,?,?)",
        (
            sku,
            "100-inch Pull-Out Sofa",
            json.dumps({"aspects": {"Item Length": ["114.6 in"]}}),
            json.dumps(
                {
                    "Assembled Length (in.)": "114.57",
                    "Assembled Width (in.)": "91.34",
                    "Assembled Height (in.)": "34.65",
                }
            ),
        ),
    )
    conn.commit()

    class SparseSnap:
        title = "100-inch Pull-Out Sofa"
        characteristics = ["Comfortable soft cushioned corduroy upholstery."]
        attributes = {
            "Main Material": "Corduroy",
            "Upholstery Material": "Corduroy",
        }
        specs = {}
        description_html = "<div>Comfortable soft cushioned corduroy upholstery.</div>"

    monkeypatch.setattr(repair_mod, "build_source_snapshot", lambda detail: SparseSnap())
    dj = types.SimpleNamespace(get_product_detail_by_sku=lambda requested_sku: {"sku": requested_sku})

    built, reason = repair_mod.build_repair(conn, dj, sku)

    assert built is not None, reason
    assert built[3].attributes["Assembled Length (in.)"] == "114.57"
    assert built[3].attributes["Assembled Width (in.)"] == "91.34"
    assert built[3].attributes["Assembled Height (in.)"] == "34.65"


def test_reconcile_semantic_aspects_removes_unsupported_legacy_claims(repair_mod):
    class Snap:
        title = "43-inch round corduroy single sofa"
        description_html = "<div>Soft corduroy upholstery with foam and spring support.</div>"
        characteristics = ["Soft corduroy upholstery with foam and spring support."]
        attributes = {
            "Main Material": "Corduroy,Foam+Spring",
            "Upholstery Material": "Corduroy",
        }

    aspects = {
        "Material": ["Fabric"],
        "Frame Material": ["Steel"],
        "Upholstery Material": ["Corduroy"],
        "Upholstery Fabric": ["Polyester"],
        "填充物": ["Foam"],
        "Features": ["With Cushion", "Soft"],
    }
    violations = [
        {"claim_type": "semantic_material", "claim_text": "polyester"},
        {"claim_type": "semantic_material", "claim_text": "steel"},
        {"claim_type": "semantic_material", "claim_text": "foam"},
        {"claim_type": "semantic_feature", "claim_text": "with cushion"},
    ]

    cleaned = repair_mod.reconcile_semantic_aspects(aspects, Snap(), violations)

    assert "Frame Material" not in cleaned
    assert cleaned["Upholstery Fabric"] == ["Corduroy"]
    assert "填充物" not in cleaned
    assert cleaned["Features"] == ["Soft"]


def test_reconcile_semantic_aspects_removes_feature_claim_from_type(repair_mod):
    class Snap:
        title = "Oversized Papasan Rocking Chair"
        description_html = "<div>Curved steel rocking base.</div>"
        characteristics = ["Gentle smooth rocking motion."]
        attributes = {"Main Material": "Rattan+Metal"}

    aspects = {
        "Type": ["Hanging Chair"],
        "Color": ["Grey"],
    }
    violations = [
        {"claim_type": "semantic_feature", "claim_text": "hanging"},
    ]

    cleaned = repair_mod.reconcile_semantic_aspects(aspects, Snap(), violations)

    assert "Type" not in cleaned
    assert cleaned["Color"] == ["Grey"]


def test_reconcile_semantic_aspects_removes_unsupported_capacity_field(repair_mod):
    class Snap:
        title = "Solid Wood Writing Desk"
        description_html = "<div>Desk with drawer and shelf.</div>"
        characteristics = ["Spacious desktop with two drawers."]
        attributes = {"Main Material": "Solid Wood"}

    aspects = {
        "Seating Capacity": ["Up to 6"],
        "Number of Drawers": ["2"],
    }
    violations = [
        {"claim_type": "semantic_capacity", "claim_text": "6 person"},
    ]

    cleaned = repair_mod.reconcile_semantic_aspects(aspects, Snap(), violations)

    assert "Seating Capacity" not in cleaned
    assert cleaned["Number of Drawers"] == ["2"]
