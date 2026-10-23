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
        ("W331P309435", None, "stored_head_fit", "20487"),
        ("W331P498875", None, "stored_head_fit", "20487"),
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


def _real_66_rows() -> list[dict]:
    extra = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "category_remap_named_publish_skus.json").read_text(
            encoding="utf-8"
        )
    )
    rows: list[dict] = []
    seen: set[str] = set()

    def _take(src):
        for row in src:
            sku = str(row.get("sku") or "")
            if not sku or sku in seen or sku.startswith("GATE66_"):
                continue
            if row.get("status") in {"SYNTHETIC", "TEST"}:
                continue
            seen.add(sku)
            rows.append(row)
            if len(rows) >= 66:
                return True
        return False

    ready_pending = [row for row in ROWS if row.get("status") in {"READY", "PENDING"}]
    if _take(ready_pending):
        return rows[:66]
    if _take(extra):
        return rows[:66]
    _take(ROWS)
    return rows[:66]


def test_ready_pending_66_mirror_zero_writes():
    """Real 66 SKUs (no GATE66_ padding): 0 writes."""
    rows = _real_66_rows()
    assert len(rows) == 66
    assert all(not str(row["sku"]).startswith("GATE66_") for row in rows)
    required = {
        "W3190S00002",
        "N707S300277B",
        "W1321S00027",
        "W2899P372741",
        "W2530P418996",
        "W465P408010",
    }
    present = {row["sku"] for row in rows}
    assert required <= present
    writes = 0
    for row in rows:
        decision = _decide(row)
        write_id = decision_category_id(decision, row.get("stored_category"), index=INDEX)
        out, _issues = _publish(row)
        if write_id or (
            str(out.get("categoryId") or "")
            and str(out.get("categoryId")) != str(row.get("stored_category") or "")
        ):
            writes += 1
    assert writes == 0


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


class _PublishPathMatcher:
    """Emulate the pre-gate batch_publish remap/canonicalize/matcher mistakes."""

    BAD = {
        "W3190S00002": ("20493", "Display Cabinets"),
        "W2275P542384": ("20493", "Display Cabinets"),
        "W1321S00027": ("20487", "Cabinets & Cupboards"),
        "W1321S00028": ("20487", "Cabinets & Cupboards"),
        "W1580S00493": ("20487", "Cabinets & Cupboards"),
        "W2899P372741": ("103430", "Armoires & Wardrobes"),
        "W2530P418996": ("79682", "Patio Chairs"),
        "T3971P401872": ("22656", "Coat & Hat Racks"),
        "W1580S00491": ("22656", "Coat & Hat Racks"),
        "W2987P351728": ("22656", "Coat & Hat Racks"),
        "W1162P407684": ("20740", "Furniture & Scratchers"),
        "W1162P407685": ("20740", "Furniture & Scratchers"),
        "N767P369236T": ("79684", "Loungers"),
    }

    def __init__(self, sku=""):
        self.sku = sku

    def canonicalize_category(self, title, category_id, category_name=None, description=None):
        if self.sku in self.BAD:
            return self.BAD[self.sku]
        hay = f"{title}"
        if "hall tree" in hay.lower() or "coat rack" in hay.lower():
            return "22656", "Coat & Hat Racks"
        if "cat litter" in hay.lower():
            return "20740", "Furniture & Scratchers"
        if "l-shaped" in hay.lower() or "l shaped" in hay.lower():
            return "20487", "Cabinets & Cupboards"
        if "sideboard" in hay.lower():
            return "103430", "Armoires & Wardrobes"
        if "rocking chair" in hay.lower() and "indoor" in hay.lower():
            return "79682", "Patio Chairs"
        if "display cabinet" in hay.lower():
            return "20493", "Display Cabinets"
        return category_id, category_name

    def get_category_and_aspects(self, title, aspects, description=None):
        cid, name = self.canonicalize_category(title, "", None, description)
        if not cid:
            cid, name = "54235", "Chairs"
        return cid, name, dict(aspects or {})

    def is_category_plausible_for_text(self, title, category_id, category_name=None):
        return bool(category_id)

    def _get_category_aspects(self, category_id):
        return [], []

    def _complete_aspects(self, existing, required, recommended, title, category_name):
        return dict(existing or {})


def _emulate_publish(row: dict) -> dict:
    from batch_publish import select_batch_publish_category

    return select_batch_publish_category(
        stored_cat=row.get("stored_category") or "",
        stored_cat_name="",
        title=row.get("live_title") or row.get("giga_title") or "",
        description="",
        existing_aspects={"Brand": ["AquaVerve"]},
        opt={"categoryId": row.get("stored_category") or ""},
        listing_status=row.get("status") or "READY",
        category_matcher=_PublishPathMatcher(row.get("sku") or ""),
        preserve_approved_category=False,
        sellable_check=lambda cid: str(cid) not in {"20493", "38200"},
    )


def test_batch_publish_path_gates_named_skus():
    extra = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "category_remap_named_publish_skus.json").read_text(
            encoding="utf-8"
        )
    )
    by_sku = {row["sku"]: row for row in ROWS}
    for row in extra:
        by_sku[row["sku"]] = row
    named = [
        "W3190S00002",
        "W1321S00027",
        "W1321S00028",
        "W1580S00493",
        "W2899P372741",
        "W2530P418996",
        "T3971P401872",
        "W1580S00491",
        "W1162P407684",
        "N767P369236T",
        "N707S300277B",
        "N707S186617W",
        "W465P408010",
    ]
    changed = 0
    for sku in named:
        row = by_sku[sku]
        result = _emulate_publish(row)
        stored = str(row.get("stored_category") or "") or None
        got = result.get("category_id")
        if sku in {"N707S300277B", "N707S186617W", "W3190S00002"}:
            assert result.get("held") is True
            assert got is None
            continue
        assert result.get("held") is False
        assert got == stored
        if got != stored:
            changed += 1
    assert changed == 0


def test_batch_publish_empty_stored_holds():
    result = _emulate_publish(
        {
            "sku": "EMPTY_FILL",
            "status": "READY",
            "stored_category": "",
            "live_title": "Outdoor Wicker Chaise Lounge Chair",
        }
    )
    assert result["held"] is True
    assert result["action"] == "hold_empty"
    assert result["category_id"] is None


def test_batch_publish_ready_pending_zero_unapproved_changes():
    rows = [row for row in ROWS if row.get("status") in {"READY", "PENDING"}]
    extra = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "category_remap_named_publish_skus.json").read_text(
            encoding="utf-8"
        )
    )
    seen = {row["sku"] for row in rows}
    for row in extra:
        if row["sku"] not in seen:
            rows.append(row)
            seen.add(row["sku"])
    changed = []
    for row in rows:
        result = _emulate_publish(row)
        stored = str(row.get("stored_category") or "") or None
        got = result.get("category_id")
        if result.get("held"):
            continue
        if got != stored:
            changed.append((row["sku"], stored, got, result.get("action")))
    assert changed == []


def test_h2_implausible_stored_is_blocker_for_ready_pending():
    row = _row("N707S300277B")
    opt = {
        "title": row["live_title"],
        "description": "<div>KEY FEATURES<ul><li>x</li></ul></div>",
        "categoryId": "177000",
        "status": "PENDING",
        "aspects": {},
    }
    issues = validate_listing_quality(opt, source_title=row.get("giga_title") or "", listing_status="PENDING")
    assert any(getattr(i, "code", None) == "stored_category_unpublishable" and i.severity == "BLOCKER" for i in issues)
    published = dict(opt)
    published["status"] = "PUBLISHED"
    pub_issues = validate_listing_quality(published, source_title=row.get("giga_title") or "", listing_status="PUBLISHED")
    assert not any(getattr(i, "code", None) == "stored_category_unpublishable" for i in pub_issues)


def test_m1_tree_unavailable_is_run_level_only(monkeypatch, caplog):
    import logging

    import src.utils.category_decision as cd

    cd.reset_tree_unavailable_warning()
    monkeypatch.setattr(cd, "_category_index", lambda index=None: {})
    with caplog.at_level(logging.WARNING):
        first = resolve_category_decision("Patio Chair", "", "", "79682", "", index={})
        second = resolve_category_decision("Kitchen Island", "", "", "177000", "", index={})
    assert first.kind == "keep"
    assert second.kind == "keep"
    assert first.reason == "tree_cache_unavailable"
    warnings = [rec.message for rec in caplog.records if "tree cache" in rec.message.lower()]
    assert len(warnings) == 1
    gated, action = cd.apply_category_write_gate("79682", "54235", "Patio Chair", listing_status="READY", index={})
    assert (gated, action) == ("79682", "keep")


def test_m2_correct_stored_leaves_are_kept():
    cases = [
        ("W465P408010", "Heavy Duty Bolt Cutter 24 Inch", "179446", "keep"),
        ("VANITY1", "24 Inch Bathroom Vanity with Sink", "32878", "keep"),
        ("CAB1", "Kitchen Storage Cabinet with Doors", "20487", "keep"),
        ("HUTCH1", "Outdoor Rabbit Hutch Wooden Cage", "63108", "keep"),
        ("MATT1", "12 Inch Memory Foam Mattress Queen", "131588", "keep"),
        ("TREAD1", "Folding Treadmill for Home Gym", "179446", "keep"),
        ("COOP1", "Wooden Chicken Coop with Nesting Box", "63108", None),
        ("TRASH1", "Outdoor Trash Can Storage Shed for 2 Bins", "161059", None),
    ]
    for sku, title, stored, expect_kind in cases:
        decision = resolve_category_decision(title, "", "", stored, "", index=INDEX)
        write_id = decision_category_id(decision, stored, index=INDEX)
        assert write_id is None, (sku, decision)
        if expect_kind == "keep":
            assert decision.kind == "keep", (sku, decision)
        ok, reason = validate_target_category(stored, "classified", title, index=INDEX)
        assert ok is True, (sku, stored, reason)


def test_matcher_does_not_emit_invalid_furniture_leaves():
    from src.services.ebay_category_matcher import EbayCategoryMatcher

    class _Oauth:
        def get_application_token(self):
            return "offline"

    matcher = EbayCategoryMatcher(_Oauth())
    assert matcher._fallback_category("Modern End Table 18 Inch")[0] != "38200"
    assert matcher._fallback_category("5-Shelf Glass Display Cabinet")[0] != "20493"
    assert matcher._fallback_category("Kitchen Island with Drop Leaf")[0] != "177000"
    assert matcher._fallback_category("Kitchen Island with Drop Leaf")[0] == "260157"


def test_edge_empty_stored_unknown_head_holds():
    decision = resolve_category_decision("Bathroom Vanity Hutch with Mirror", "", "", "", "", index=INDEX)
    assert decision.kind == "unknown"
    assert decision_category_id(decision, "", index=INDEX) is None
    gated, action = __import__("src.utils.category_decision", fromlist=["apply_category_write_gate"]).apply_category_write_gate(
        "", "32878", "Bathroom Vanity Hutch with Mirror", listing_status="READY", index=INDEX
    )
    assert (gated, action) == (None, "hold_empty")


def test_edge_chicken_coop_and_trash_storage_validate():
    ok, reason = validate_target_category("63108", "classified", "Wooden Chicken Coop", index=INDEX)
    assert ok is True, reason
    ok, reason = validate_target_category("161059", "classified", "Trash Can Storage Shed", index=INDEX)
    assert ok is True, reason


def test_edge_blacklist_emptiness_respects_applies_to_stores(tmp_path, monkeypatch):
    from src.utils import publish_fee_preview

    path = tmp_path / "bl.json"
    path.write_text(
        '{"applies_to_stores": ["other-store"], "publish_skip_category_ids": ["6001"]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(publish_fee_preview, "_current_store_aliases", lambda: {"main", "aquaverve"})
    assert publish_fee_preview.blacklist_file_is_empty(str(path)) is True
    path2 = tmp_path / "bl2.json"
    path2.write_text(
        '{"applies_to_stores": ["main"], "publish_skip_category_ids": ["6001"]}',
        encoding="utf-8",
    )
    assert publish_fee_preview.blacklist_file_is_empty(str(path2)) is False
