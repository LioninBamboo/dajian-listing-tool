"""High-fee gate, autofix whitelist, count leftovers, picker day-cut, nulls."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import sys

_PKG = Path(__file__).resolve().parents[1] / "packages" / "listing_qc"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from listing_qc.autofix_whitelist import (  # noqa: E402
    MISSING_VIDEO_AUTOFIX_FIX_KEYS,
    SCHEDULED_SOURCE_ASPECT_AUTOFIX_FIX_KEYS,
    SEMANTIC_REWRITE_AUTOFIX_FIX_KEYS,
)
from src.services.semantic_rewrite import SEMANTIC_REWRITE_AUTOFIX_FIX_KEYS as REWRITE_KEYS
from src.utils.listing_fact_sheet import compare_fact_sheets, parse_noun_count
from src.utils.publish_fee_preview import (
    HIGH_FEE_PATH_SEGMENTS,
    high_fee_category_block_reason,
)


def test_category_id_absent_from_every_automatic_fix_key_set():
    for name, keys in (
        ("source_aspect", SCHEDULED_SOURCE_ASPECT_AUTOFIX_FIX_KEYS),
        ("missing_video", MISSING_VIDEO_AUTOFIX_FIX_KEYS),
        ("semantic_rewrite_pkg", SEMANTIC_REWRITE_AUTOFIX_FIX_KEYS),
        ("semantic_rewrite_svc", REWRITE_KEYS),
    ):
        assert "categoryId" not in keys, name
        assert "category_id" not in keys, name


def test_high_fee_path_segment_blocks_even_when_blacklist_empty(tmp_path, monkeypatch):
    from src.utils import publish_fee_preview

    monkeypatch.setattr(publish_fee_preview, "REPO_ROOT", tmp_path)
    publish_fee_preview._EMPTY_BLACKLIST_WARNED.clear()
    reason = high_fee_category_block_reason(
        category_id="99999",
        category_path="eBay Motors > Other Vehicles & Trailers > Trailers",
    )
    assert reason and reason.startswith("BLOCKED_HIGH_FEE")
    assert HIGH_FEE_PATH_SEGMENTS


def test_high_fee_twenty_dollar_preview_fails_closed():
    reason = high_fee_category_block_reason(
        category_id="38204",
        listing_fees={
            "feeSummaries": [
                {
                    "fees": [
                        {"feeType": "INSERTION_FEE", "amount": {"value": "20.00", "currency": "USD"}}
                    ]
                }
            ]
        },
    )
    assert reason and reason.startswith("BLOCKED_HIGH_FEE")


def test_apply_approved_fixes_blocks_high_fee_category():
    from scripts import apply_approved_fixes as aaf

    blocked = aaf.high_fee_category_block_reason(
        category_id="1",
        category_name="Trailers",
    )
    assert blocked and blocked.startswith("BLOCKED_HIGH_FEE")


def test_fix_listing_on_ebay_blocks_high_fee_category_write(monkeypatch):
    from scripts import audit_fix_active_listings

    monkeypatch.setattr(
        "src.utils.publish_fee_preview.high_fee_category_block_reason",
        lambda **kwargs: "BLOCKED_HIGH_FEE: trailers",
    )

    class _Client:
        pass

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU1",
        {
            "title": "Trailer",
            "description": "",
            "optimization": json.dumps({"title": "Trailer", "categoryId": "38204", "aspects": {}}),
            "attributes": "{}",
            "specs": "{}",
        },
        {"categoryId": "6001"},
        _Client(),
        None,
    )
    assert results and str(results[0]).startswith("BLOCKED_HIGH_FEE")


def test_count_double_sided_and_double_bed_drawers_are_not_two():
    assert parse_noun_count("double-sided shelves", "shelf") != 2
    assert parse_noun_count("two-sided shelves", "shelf") != 2
    assert parse_noun_count("Double Bed with Drawers", "drawer") != 2
    assert parse_noun_count("double door cabinet with drawer", "drawer") != 2


def test_count_two_3_tier_shelves_is_never_three():
    parsed = parse_noun_count("two 3-tier shelves", "shelf")
    assert parsed != 3
    assert parsed in {2, None}


def test_legacy_no_text_compare_emits_count_unverifiable_not_semantic_count():
    source = {
        "materials": [],
        "features": [],
        "counts": {"drawers": 3},
        "capacity": None,
        "certifications": [],
        "dimensions": {},
    }
    live = dict(source, counts={"drawers": 1})
    violations = compare_fact_sheets(source, live, live_text=None, source_text=None)
    assert not any(v["claim_type"] == "semantic_count" for v in violations)
    assert any(v["claim_type"] == "count_unverifiable" and v["severity"] == "INFO" for v in violations)


def test_picker_same_day_after_six_rejects_yesterday(tmp_path):
    from scripts.semantic_rewrite import pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    yesterday = tmp_path / "listing_audit_fix_20261021_113000.json"
    yesterday.write_text(
        json.dumps(
            {
                "mode": "live_audit",
                "scope": "full",
                "total_published": 202,
                "generated_at": (now - timedelta(hours=24)).isoformat(),
                "issues": [{"sku": "OLD", "issues": [{"type": "x"}]}],
            }
        ),
        encoding="utf-8",
    )
    assert pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now) is None
    picked = pick_latest_daily_live_audit(
        list(tmp_path.glob("*.json")), now=now, allow_yesterday=True
    )
    assert picked is not None
    assert picked[0] == yesterday


def test_picker_zero_issues_has_distinct_skip_message(tmp_path):
    from scripts.semantic_rewrite import last_daily_audit_skip_message, pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 12, 10, 0)
    path = tmp_path / "listing_audit_fix_20261022_113009.json"
    path.write_text(
        json.dumps(
            {
                "mode": "live_audit",
                "scope": "full",
                "total_published": 217,
                "generated_at": (now - timedelta(hours=1)).isoformat(),
                "issues": [],
            }
        ),
        encoding="utf-8",
    )
    assert pick_latest_daily_live_audit([path], now=now) is None
    msg = last_daily_audit_skip_message()
    assert msg.startswith("SKIP: fresh daily live_audit has 0 issues")
    assert path.name in msg


def test_picker_newer_unparseable_fails_closed(tmp_path):
    from scripts.semantic_rewrite import last_daily_audit_skip_message, pick_latest_daily_live_audit

    now = datetime(2026, 10, 22, 13, 0, 0)
    good = tmp_path / "listing_audit_fix_20261022_113009.json"
    good.write_text(
        json.dumps(
            {
                "mode": "live_audit",
                "scope": "full",
                "total_published": 217,
                "generated_at": datetime(2026, 10, 22, 11, 30, 9).isoformat(),
                "issues": [{"sku": "S1", "issues": [{"type": "x"}]}],
            }
        ),
        encoding="utf-8",
    )
    bad = tmp_path / "listing_audit_fix_20261022_125000.json"
    bad.write_text("{not-json", encoding="utf-8")
    assert pick_latest_daily_live_audit(list(tmp_path.glob("*.json")), now=now) is None
    assert "unparseable" in last_daily_audit_skip_message()


def test_resolve_nulls_and_non_str_do_not_raise():
    from src.utils.category_decision import resolve_category_decision

    for args in (
        (None, None, None, None, None),
        ("", "", "", "", ""),
        (1, 2, 3, 4, 5),
        ({}, [], None, "", {}),
    ):
        decision = resolve_category_decision(*args)
        assert decision.kind in {"unknown", "keep"}
        assert decision.category_id is None


def test_legacy_optimization_list_stays_empty_dict():
    from scripts.audit_fix_active_listings import parse_json

    assert parse_json("[1, 2]") == {}
    assert parse_json("not-json") == {}
    assert parse_json(None) == {}
