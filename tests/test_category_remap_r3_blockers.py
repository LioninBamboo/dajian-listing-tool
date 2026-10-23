"""PR #27 follow-up: items 1–6 (and optional 7) against the fixture tree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.utils.category_decision import (
    APPROVED_AUTO_REMAPS,
    CategoryDecision,
    decision_category_id,
    is_approved_auto_remap,
    resolve_category_decision,
    validate_target_category,
)
from src.utils.listing_quality_gate import (
    ListingQualityIssue,
    normalize_generated_listing,
    validate_listing_quality,
)
from src.utils.product_context_signals import first_product_noun, unknown_head_noun
from tests.conftest import load_fixture_category_index

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "category_remap_real_skus.json"
ROWS = json.loads(FIXTURE.read_text(encoding="utf-8"))
INDEX = load_fixture_category_index()

FORBIDDEN_WRITES = {"38200", "20493", "261263"}


def _row(sku: str) -> dict:
    for row in ROWS:
        if row["sku"] == sku:
            return row
    raise AssertionError(sku)


def _decide(row: dict) -> CategoryDecision:
    return resolve_category_decision(
        row.get("live_title"),
        row.get("giga_title"),
        "",
        row.get("stored_category"),
        "",
        index=INDEX,
    )


def _publish(row: dict) -> tuple[dict, list[ListingQualityIssue]]:
    stored = str(row.get("stored_category") or "")
    opt = {
        "title": row.get("live_title") or "",
        "description": "<div>KEY FEATURES<ul><li>x</li></ul></div>",
        "categoryId": stored,
        "aspects": {},
    }
    out = normalize_generated_listing(opt, source_title=row.get("giga_title") or "")
    issues = validate_listing_quality(
        out,
        source_title=row.get("giga_title") or "",
        source_description="",
    )
    return out, issues


@pytest.mark.parametrize(
    "sku,proposal,reason_prefix,write",
    [
        ("WF321204AAD", "79684", "chaise", None),
        ("WF321204AAE", "79684", "chaise", None),
        ("N770P281696C", "79684", "chaise", None),
        ("W2887P511377", None, "outdoor_chair_lock", "79682"),
        ("W2887P511379", None, "outdoor_chair_lock", "79682"),
        ("W2887P511381", None, "outdoor_chair_lock", "79682"),
        ("N770P272405T", "79684", "chaise", None),
        ("W331P309435", None, "unknown_head_noun", "20487"),
        ("W331P498875", None, "", "20487"),
        ("W2263P395490", None, "", "20488"),
        ("N770P253018C", None, "accessory", None),
        ("W1162P407684", "20740", "", "20751"),
        ("W1162P407685", "20740", "", "20751"),
        ("W3190S00002", None, "invalid_target_leaf", "125085"),
        ("W2275P542384", None, "invalid_target_leaf", "125085"),
        ("W5368P497699", None, "invalid_target_leaf", "38199"),
        ("N770P248135N", None, "invalid_target_leaf", "54235"),
        ("W5709P565684", None, "", "54235"),
        ("N707S300277B", None, "stored_category_implausible", "177000"),
        ("N707S186617W", None, "stored_category_implausible", "177000"),
        ("T3971P401872", "22656", "hall_tree", "103431"),
        ("W1580S00491", "22656", "hall_tree", "20487"),
        ("W2899P372739", "183322", "sideboard", "20580"),
        ("N759S006011B", "107578", "", None),
        ("W640P443554", None, "", "54235"),
        ("W2987P351728", "22656", "", "32880"),
    ],
)
def test_real_sku_table_r3(sku, proposal, reason_prefix, write):
    row = _row(sku)
    decision = _decide(row)
    write_id = decision_category_id(decision, row.get("stored_category"), index=INDEX)
    out, issues = _publish(row)
    stored = str(row.get("stored_category") or "") or None
    published = str(out.get("categoryId") or "") or None

    assert write_id is None
    assert published == stored
    assert published not in FORBIDDEN_WRITES
    if proposal:
        assert decision.category_id == proposal
    else:
        assert decision.category_id in {None, stored}
    if reason_prefix:
        assert decision.reason.startswith(reason_prefix) or reason_prefix in decision.reason
    if write is None and not stored:
        assert any(
            getattr(i, "code", None) in {"category_remap_pending_review", "missing_category"}
            or (isinstance(i, dict) and i.get("type") in {"category_remap_pending_review", "missing_category"})
            for i in issues
        ) or decision.kind == "unknown"
    if sku in {"N707S300277B", "N707S186617W"}:
        assert decision.reason == "stored_category_implausible"
        assert decision.suggested_alternative == "260157"
    if sku in {"W3190S00002", "W2275P542384"}:
        assert decision.suggested_alternative == "20487"
    if sku in {"W5368P497699", "N770P248135N"}:
        assert decision.suggested_alternative == "38204"
    if sku in {"W1162P407684", "W1162P407685"}:
        assert decision.category_id != "20744"
        assert "20744" not in {published, write_id}


def test_synthetic_s1_to_s6_and_extra_edges():
    cases = [
        (
            "Outdoor Sun Lounger Chaise Cushion Only Waterproof",
            "",
            None,
            "accessory",
            {"79684", "79682"},
        ),
        (
            "Bathroom Vanity Hutch with Mirror 30 Inch Buffet Style",
            "",
            None,
            "unknown_head_noun",
            {"183322"},
        ),
        (
            "Outdoor Adirondack Chair Set of 2 Acacia Wood",
            "",
            "79682",
            "",
            {"79684"},
        ),
        (
            "47 Inch Chairs Stable Tablet Stand",
            "",
            None,
            "",
            {"38200", "38204", "112590"},
        ),
        (
            "Modern End Table",
            "38200",
            None,
            "stored_category_implausible",
            {"38200"},
        ),
        (
            "Hutch Desk with Drawers 48 Inch Oak",
            "",
            "88057",
            "",
            {"183322"},
        ),
        (
            "Mid Century Lounge Sofa Velvet",
            "38208",
            None,
            "",
            {"79684"},
        ),
        (
            "NATURAL WOOD TV-STAND 59 INCH SIDEBOARD CABINET",
            "20488",
            None,
            "",
            {"183322"},
        ),
    ]
    for title, stored, proposal, reason, must_not in cases:
        decision = resolve_category_decision(title, "", "", stored, "", index=INDEX)
        write_id = decision_category_id(decision, stored, index=INDEX)
        assert write_id is None, (title, write_id, decision)
        if proposal:
            assert decision.category_id == proposal, (title, decision)
        else:
            assert decision.category_id in {None, stored}, (title, decision)
        if reason:
            assert decision.reason == reason or reason in decision.reason, (title, decision)
        assert (decision.category_id or "") not in must_not or decision.kind in {"keep", "unknown"}
        if title == "Modern End Table":
            assert decision.suggested_alternative == "38204"
        if "Cushion Only" in title:
            assert first_product_noun(title) in {"cushion", "chaise lounge cushion", "lounger cushion"} or unknown_head_noun(
                first_product_noun(title)
            )


def test_s6_dog_crate_never_cat_litter():
    decision = resolve_category_decision(
        "Dog Crate Furniture End Table",
        "",
        "",
        "",
        "",
        index=INDEX,
    )
    ok, reason = validate_target_category("100411", "cat_litter", "Dog Crate Furniture End Table", index=INDEX)
    assert ok is False
    assert reason == "pet_species_mismatch"
    write_id = decision_category_id(decision, "", index=INDEX)
    assert write_id != "100411"
    assert decision.category_id != "100411"


def test_s7_approved_auto_remap_writes_only_that_pair(monkeypatch):
    import src.utils.category_decision as cd

    monkeypatch.setattr(
        cd,
        "APPROVED_AUTO_REMAPS",
        frozenset({("20580", "183322")}),
    )
    approved = resolve_category_decision(
        "Elegant 34 Inch Sideboard Cabinet with Mirrored Doors",
        "",
        "",
        "20580",
        "",
        index=INDEX,
    )
    assert approved.category_id == "183322"
    assert cd.decision_category_id(approved, "20580", index=INDEX) == "183322"
    other = resolve_category_decision(
        "Corner Coat Rack Hall Tree with Shoe Rack and Shelves 71 Inch",
        "",
        "",
        "103431",
        "",
        index=INDEX,
    )
    assert other.category_id == "22656"
    assert cd.decision_category_id(other, "103431", index=INDEX) is None


def test_s8_high_fee_motors_prefix_not_collectible_boats():
    from src.utils.publish_fee_preview import category_is_high_fee

    assert category_is_high_fee(category_path="eBay Motors > Boats") is True
    assert category_is_high_fee(category_path="Collectibles > Transportation > Boats & Ships") is False


def test_ready_pending_66_mirror_zero_writes():
    """Fixture rows that mirror the 66 READY/PENDING remaps: 0 writes, 66 flagged."""
    templates = [
        ("Outdoor Wicker Chaise Lounge Chairs Set of 2", "", "PENDING"),
        ("Outdoor Adirondack Chair Set of 2 Acacia Wood", "", "PENDING"),
        ("Corner Coat Rack Hall Tree with Shoe Rack", "103431", "PENDING"),
        ("Kitchen Pantry Hutch Storage Cabinet Buffet", "20487", "PENDING"),
        ("Cat Litter Cabinet with Cat Tree Walnut", "20751", "PENDING"),
        ("5-Shelf Glass Display Cabinet 71 Inch Tall", "125085", "PENDING"),
        ("Kitchen Island with Drop Leaf Power Outlet", "177000", "PENDING"),
        ("Elegant 34 Inch Sideboard Cabinet", "20580", "PENDING"),
        ("5-Piece Dining Set 1 Table 4 Chairs", "", "PENDING"),
        ("Coat Racks Solid Wood Hanger Freestanding", "32880", "READY"),
        ("Outdoor Chaise Lounge Chair Adjustable", "", "PENDING"),
        ("4-in-1 Detachable Hall Tree with Storage", "20487", "READY"),
    ]
    rows = []
    i = 0
    while len(rows) < 66:
        title, stored, status = templates[i % len(templates)]
        rows.append(
            {
                "sku": f"GATE66_{len(rows):02d}",
                "status": status,
                "stored_category": stored,
                "live_title": f"{title} variant {len(rows)}",
                "giga_title": "",
            }
        )
        i += 1
    writes = 0
    flagged = 0
    for row in rows:
        decision = _decide(row)
        write_id = decision_category_id(decision, row["stored_category"], index=INDEX)
        out, issues = _publish(row)
        if write_id or (
            str(out.get("categoryId") or "")
            and str(out.get("categoryId")) != str(row["stored_category"] or "")
        ):
            writes += 1
        codes = {getattr(i, "code", None) or (i.get("type") if isinstance(i, dict) else None) for i in issues}
        if (
            "category_remap_pending_review" in codes
            or "missing_category" in codes
            or decision.kind == "unknown"
            or (decision.category_id and decision.category_id != row["stored_category"])
        ):
            flagged += 1
    assert writes == 0
    assert flagged == 66


def test_audit_and_publish_still_agree_on_fixture(monkeypatch):
    from tests.test_category_remap_real_skus import test_real_sku_audit_and_publish_agree

    for row in ROWS:
        test_real_sku_audit_and_publish_agree(row, monkeypatch)


def test_validate_never_emits_forbidden_leaves():
    for cid in FORBIDDEN_WRITES:
        ok, reason = validate_target_category(cid, "classified", "Side Table", index=INDEX)
        assert ok is False
        assert reason in {"invalid_target_leaf", "implausible_target_path"}
    ok, reason = validate_target_category("177000", "kitchen_island", "Kitchen Island Cart", index=INDEX)
    assert ok is False
    assert reason == "implausible_target_path"


def test_empty_approved_list_is_shipped():
    assert APPROVED_AUTO_REMAPS == frozenset()
    assert is_approved_auto_remap("20580", "183322", "sideboard_head") is False


def test_first_product_noun_word_boundaries():
    assert first_product_noun("47 Inch Chairs Stable Tablet Stand") not in {"table", "side table", "end table"}
    head = first_product_noun("Kitchen Pantry Hutch Storage Cabinet, Farmhouse Kitchen Hutch Buffet Cabinet")
    assert unknown_head_noun(head) in {"hutch", "pantry", "cabinet"} or (head and head.startswith("__unknown_head__"))
    assert first_product_noun("Natural Wood TV Stand 59 Inch Sideboard Cabinet") == "tv stand"
    assert first_product_noun("NATURAL WOOD TV-STAND 59 INCH SIDEBOARD") == "tv stand"
