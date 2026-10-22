"""Spec §4: every Appendix A row through BOTH audit and publish paths.

KEEP = stored category unchanged (no category_mismatch / no categoryId write).
NONE = no category assigned (unknown).
The two engines must return the identical leaf for every row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.utils.category_decision import (
    CategoryDecision,
    decision_category_id,
    resolve_category_decision,
)
from src.utils.listing_quality_gate import (
    normalize_generated_listing,
    normalize_listing_optimization,
)
from src.utils.product_context_signals import as_safe_category_id

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "category_remap_real_skus.json"
ROWS = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _decision_leaf(row: dict) -> tuple[str | None, CategoryDecision]:
    stored = as_safe_category_id(row.get("stored_category"))
    decision = resolve_category_decision(
        row.get("live_title"),
        row.get("giga_title"),
        "",
        stored,
        "",
    )
    write_id = decision_category_id(decision, stored)
    if write_id:
        return write_id, decision
    if stored:
        return stored, decision
    return None, decision


def _publish_leaf(row: dict) -> str | None:
    stored = as_safe_category_id(row.get("stored_category"))
    opt = {
        "title": row.get("live_title") or row.get("giga_title") or "",
        "description": "<div>KEY FEATURES<ul><li>x</li></ul></div>",
        "categoryId": stored,
        "aspects": {},
    }
    out = normalize_generated_listing(
        opt,
        source_title=row.get("giga_title") or "",
        source_description="",
    )
    return as_safe_category_id(out.get("categoryId")) or None


def _audit_leaf(row: dict, monkeypatch) -> tuple[str | None, list]:
    from scripts import audit_fix_active_listings

    stored = as_safe_category_id(row.get("stored_category"))

    class _Matcher:
        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            from src.services.ebay_category_matcher import EbayCategoryMatcher

            class _Oauth:
                def get_application_token(self):
                    return "offline"

            return EbayCategoryMatcher(_Oauth()).canonicalize_category(
                title_context, category_id, category_name, ""
            )

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

        def get_category_and_aspects(self, title, aspects, description):
            raise AssertionError("API suggest must not invent a leaf")

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())
    opt = {
        "title": row.get("live_title") or "",
        "categoryId": stored,
        "aspects": {
            "Material": ["Wood"],
            "Item Length": ["30 in"],
            "Item Width": ["12 in"],
            "Item Height": ["29 in"],
        },
        "description": "<div>AQUAVERVE KEY FEATURES <li>x</li> SPECIFICATIONS 30 12 29 Ships from California, USA</div>",
    }
    issues, fixes = audit_fix_active_listings.audit_single_product(
        row["sku"],
        row.get("giga_title") or "",
        "{}",
        "{}",
        json.dumps(opt),
        "src",
    )
    if "categoryId" in fixes:
        return as_safe_category_id(fixes["categoryId"]), issues
    return stored or None, issues


def _outcome(leaf: str | None, stored: str) -> str:
    if leaf is None or leaf == "":
        return "NONE"
    if stored and leaf == stored:
        return "KEEP"
    return leaf


@pytest.mark.parametrize("row", ROWS, ids=[r["sku"] for r in ROWS])
def test_real_sku_audit_and_publish_agree(row, monkeypatch):
    stored = as_safe_category_id(row.get("stored_category"))
    expected = set(row["expected"])
    must_not = set(row["must_not"])

    decision_leaf, decision = _decision_leaf(row)
    publish_leaf = _publish_leaf(row)
    audit_leaf, issues = _audit_leaf(row, monkeypatch)

    assert publish_leaf == audit_leaf, (
        row["sku"],
        "publish",
        publish_leaf,
        "audit",
        audit_leaf,
        decision.reason,
    )
    # The shared decision must match both engines (KEEP/NONE collapse to stored/None).
    assert decision_leaf == publish_leaf, (row["sku"], decision_leaf, publish_leaf, decision)

    outcome = _outcome(publish_leaf, stored)
    assert outcome in expected or (publish_leaf in expected), (
        row["sku"],
        outcome,
        publish_leaf,
        expected,
        row.get("note"),
    )
    assigned = publish_leaf if outcome not in {"KEEP", "NONE"} else None
    if assigned:
        assert assigned not in must_not, (row["sku"], assigned, must_not)
    if outcome == "KEEP":
        assert not any(i.get("type") == "category_mismatch" for i in issues), (row["sku"], issues)
    if row["sku"] == "W3835P484082":
        assert decision.conflict or decision.reason == "title_source_conflict"
        assert any(i.get("type") == "title_source_conflict" for i in issues)


def test_normalize_listing_optimization_is_the_publish_path():
    assert normalize_listing_optimization is normalize_generated_listing


def test_synthetic_n_piece_chair_only_is_not_dining_set():
    leaf, decision = _decision_leaf(
        {
            "live_title": "5 Piece Outdoor Dining Chairs Set, Wicker",
            "giga_title": "",
            "stored_category": "",
        }
    )
    assert leaf in {None, "79684"}
    assert leaf != "139849"
    assert decision.kind != "dining_set"


def test_synthetic_chair_covers_are_unknown():
    leaf, decision = _decision_leaf(
        {
            "live_title": "Patio Dining Chair Covers Set of 2, Waterproof",
            "giga_title": "",
            "stored_category": "79684",
        }
    )
    assert decision.kind in {"unknown", "keep"}
    assert leaf not in {"54235"}
    if decision.kind == "unknown":
        assert decision_category_id(decision, "79684") is None


def test_synthetic_club_chair_room_list_is_not_outdoor():
    leaf, decision = _decision_leaf(
        {
            "live_title": "Upholstered Club Chair for Living Room, Patio, Garden",
            "giga_title": "",
            "stored_category": "38208",
        }
    )
    assert leaf in {None, "38208"}
    assert leaf != "79684"


def test_edge_html_and_int_category_do_not_raise():
    decision = resolve_category_decision(
        "<b>Solid Wood End Table</b>",
        12345,
        {"html": True},
        38200,
        None,
    )
    assert decision.kind in {"side_table", "keep", "unknown"}
    assert decision.category_id in {None, "38200"}


def test_edge_outdoor_stored_chair_not_downgraded_by_rattan():
    decision = resolve_category_decision(
        "PE Wicker Stacking Armchair 4PCS",
        "PE Wicker Stacking Armchair 4PCS",
        "",
        "79684",
        "Outdoor Chairs",
    )
    assert decision_category_id(decision, "79684") is None
    assert decision.category_id not in {"38208"}


def test_edge_2_pack_covers_vs_2_piece_dining_set():
    covers = resolve_category_decision(
        "2 Pack Patio Chair Covers Waterproof",
        "",
        "",
        "",
        "",
    )
    dining = resolve_category_decision(
        "2-Piece Patio Dining Table and Chairs",
        "",
        "",
        "107578",
        "",
    )
    assert covers.kind == "unknown"
    assert decision_category_id(covers, "") is None
    assert dining.category_id == "139849"
