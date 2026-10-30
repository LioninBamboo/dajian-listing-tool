"""PR-H: non-standard audit severities must not crash daily listing audit.

Normalized category findings stay report-only: they are ranked and reported,
and they must not mark a listing dirty, trigger a fix, or write the row.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.audit_severity import (  # noqa: E402
    CANONICAL,
    is_report_only_issue,
    normalize_severity,
    report_only_issue_types,
)
from src.utils.category_decision import NEEDS_MANUAL_MOTORS_LEAF_REVIEW  # noqa: E402

COAT_RACK_TITLE = "Solid Wood Rolling Coat Rack with Storage Basket"
W5368_SKU = "W5368P515524"
W5368_LISTING = "366574772360"
W5368_ISSUE = {
    "type": "category_remap_pending_review",
    "severity": "WARNING",
    "current": "103430",
    "expected": "22656",
    "detail": "remap 103430 → 22656 reason=coat_rack_head",
}
TITLE_CONFLICT_ISSUE = {
    "type": "title_source_conflict",
    "severity": "INFO",
    "current": "38208",
    "expected": None,
    "detail": "Live title and GIGA title disagree on the product leaf; holding stored category 38208",
}
GATE_FLAG_ISSUE = {
    "type": NEEDS_MANUAL_MOTORS_LEAF_REVIEW,
    "severity": "INFO",
    "current": "20487",
    "detail": NEEDS_MANUAL_MOTORS_LEAF_REVIEW,
}

_SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def _legacy_max_severity(issues: list[dict]) -> str:
    """Pre-fix ranking: drop any severity outside the canonical tuple, then min()."""
    return _SEVERITY_ORDER[
        min(
            _SEVERITY_ORDER.index(issue["severity"])
            for issue in issues
            if issue.get("severity") in _SEVERITY_ORDER
        )
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CRITICAL", "CRITICAL"),
        ("critical", "CRITICAL"),
        (" BLOCKER ", "CRITICAL"),
        ("blocker", "CRITICAL"),
        ("HIGH", "HIGH"),
        ("high", "HIGH"),
        ("ERROR", "HIGH"),
        ("error", "HIGH"),
        ("MEDIUM", "MEDIUM"),
        ("medium", "MEDIUM"),
        ("WARNING", "MEDIUM"),
        ("warning", "MEDIUM"),
        ("WARN", "MEDIUM"),
        ("warn", "MEDIUM"),
        ("LOW", "LOW"),
        ("low", "LOW"),
        ("INFO", "LOW"),
        ("info", "LOW"),
        ("FOO", "LOW"),
        ("", "LOW"),
        ("   ", "LOW"),
        (None, "LOW"),
    ],
)
def test_normalize_severity_mapping_table(raw, expected):
    assert normalize_severity(raw) == expected
    assert normalize_severity(raw) in CANONICAL


def test_report_only_helper_covers_flag_and_known_types():
    assert is_report_only_issue(dict(W5368_ISSUE)) is True
    assert is_report_only_issue(dict(TITLE_CONFLICT_ISSUE)) is True
    assert is_report_only_issue(dict(GATE_FLAG_ISSUE)) is True
    assert is_report_only_issue({"type": "custom_note", "severity": "LOW", "report_only": True}) is True
    assert is_report_only_issue({"type": "category_mismatch", "severity": "CRITICAL"}) is False
    assert "title_source_conflict" in report_only_issue_types()
    assert "category_remap_pending_review" in report_only_issue_types()
    assert NEEDS_MANUAL_MOTORS_LEAF_REVIEW in report_only_issue_types()
    assert "pin_mismatch" in report_only_issue_types()


def _insert_published(db_path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT,
            title TEXT,
            attributes TEXT,
            specs TEXT,
            optimization TEXT,
            description TEXT,
            images TEXT,
            videos TEXT,
            price REAL,
            suggested_price REAL,
            listing_id TEXT,
            status TEXT
        )
        """
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO collected_products (
                sku, title, attributes, specs, optimization, description,
                images, videos, price, suggested_price, listing_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["sku"],
                row.get("title") or COAT_RACK_TITLE,
                row.get("attributes") or "{}",
                row.get("specs") or "{}",
                row["optimization"],
                row.get("description") or "<div>Source description</div>",
                row.get("images") or "[]",
                row.get("videos") or "[]",
                row.get("price", 49.0),
                row.get("suggested_price", 59.0),
                row["listing_id"],
                "PUBLISHED",
            ),
        )
    conn.commit()
    conn.close()


def _pending_opt(listing_id: str, *, category_id: str = "103430") -> str:
    return json.dumps(
        {
            "title": COAT_RACK_TITLE,
            "description": "<div>KEY FEATURES<ul><li>Storage basket</li></ul></div>",
            "aspects": {"Brand": ["AquaVerve"]},
            "categoryId": category_id,
            "_quality_gate": {
                "version": 1,
                "ruleset_version": 2,
                "status": "pending_verify",
                "listing_id": listing_id,
                "source_fingerprint": "pending-source",
                "live_fingerprint": "pending-live",
                "updated_at": "2026-10-23T13:25:00",
            },
        },
        ensure_ascii=False,
    )


def _optimization_by_sku(db_path: Path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT sku, optimization FROM collected_products").fetchall()
    conn.close()
    return {sku: optimization for sku, optimization in rows}


def _install_live_stubs(monkeypatch, issues_by_sku: dict[str, list[dict]], *, fixes_by_sku=None):
    import daily_tasks
    from scripts import audit_fix_active_listings

    fixes_by_sku = fixes_by_sku or {}
    calls = {"fix": [], "persist": []}

    def fake_fetch(_client, sku, expected_listing_id=None):
        return (
            {
                "product": {
                    "title": COAT_RACK_TITLE,
                    "aspects": {},
                    "imageUrls": ["https://example.com/a.jpg"],
                    "videoIds": [],
                }
            },
            {
                "offerId": f"offer-{sku}",
                "listingDescription": "<div>KEY FEATURES<ul><li>x</li></ul></div>",
                "categoryId": "103430",
                "listing": {"listingId": expected_listing_id},
            },
        )

    def fake_audit(sku, *_args, **_kwargs):
        return list(issues_by_sku[sku]), dict(fixes_by_sku.get(sku) or {})

    def spy_fix(sku, *_args, **_kwargs):
        calls["fix"].append(sku)
        return ["should-not-run"]

    def spy_persist(_conn, sku, **kwargs):
        calls["persist"].append((sku, kwargs.get("state")))
        return False

    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", monkeypatch.PROJECT_ROOT, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys.modules["src.clients.real_ebay_client"],
        "create_real_ebay_client",
        lambda _env: object(),
        raising=False,
    )
    monkeypatch.setattr(audit_fix_active_listings, "_fetch_live_listing_context", fake_fetch)
    monkeypatch.setattr(audit_fix_active_listings, "audit_single_product", fake_audit)
    monkeypatch.setattr(audit_fix_active_listings, "fix_listing_on_ebay", spy_fix)
    monkeypatch.setattr(audit_fix_active_listings, "persist_listing_audit_state", spy_persist)
    return calls


@pytest.fixture
def project_root(monkeypatch, tmp_path):
    monkeypatch.PROJECT_ROOT = tmp_path
    # Importing daily_tasks pulls the real eBay client module; make sure the
    # name the in-function import resolves is the one we patch.
    import src.clients.real_ebay_client  # noqa: F401

    return tmp_path


def _run(auto_fix: bool):
    import daily_tasks

    return daily_tasks.run_listing_audit(
        auto_fix=auto_fix,
        use_live=True,
        record_clean_state=True,
    )


def _assert_reported(summary: dict, sku: str, max_severity: str) -> None:
    assert "error" not in summary, summary
    matches = [row for row in summary["top_issues"] if row["sku"] == sku]
    assert matches, summary
    assert matches[0]["max_severity"] == max_severity, summary


def test_w5368_and_report_only_siblings_do_not_fix_or_write(monkeypatch, project_root):
    """Incident row plus the INFO siblings: ranked, not written.

    ``category_remap_pending_review`` / ``title_source_conflict`` (and the
    motors gate flag) stay report-only after WARNING/INFO is normalized.
    """
    rows = [
        {
            "sku": W5368_SKU,
            "title": COAT_RACK_TITLE,
            "listing_id": W5368_LISTING,
            "optimization": _pending_opt(W5368_LISTING),
        },
        {
            "sku": "TITLE-CONFLICT",
            "title": "Outdoor Chair Mid-Century Modern",
            "listing_id": "222",
            "optimization": _pending_opt("222", category_id="38208"),
        },
        {
            "sku": "GATE-FLAG",
            "title": "Bar Cabinet",
            "listing_id": "333",
            "optimization": _pending_opt("333", category_id="20487"),
        },
    ]
    issues = {
        W5368_SKU: [dict(W5368_ISSUE)],
        "TITLE-CONFLICT": [dict(TITLE_CONFLICT_ISSUE)],
        "GATE-FLAG": [dict(GATE_FLAG_ISSUE)],
    }
    expected = {
        W5368_SKU: "MEDIUM",
        "TITLE-CONFLICT": "LOW",
        "GATE-FLAG": "LOW",
    }

    for auto_fix in (False, True):
        db_path = project_root / "ebay_collection.db"
        if db_path.exists():
            db_path.unlink()
        _insert_published(db_path, rows)
        before = _optimization_by_sku(db_path)
        calls = _install_live_stubs(monkeypatch, issues)
        summary = _run(auto_fix)

        assert summary["critical"] == 0, summary
        assert summary["fixed"] == 0, summary
        assert summary["issues_found"] == 3, summary
        for sku, severity in expected.items():
            _assert_reported(summary, sku, severity)
        assert calls["fix"] == [], calls
        assert calls["persist"] == [], calls
        assert _optimization_by_sku(db_path) == before


def test_normalized_real_issues_still_do_not_fix_or_write(monkeypatch, project_root):
    """The objects ``audit_single_product`` actually returns do not write."""
    coat_issues, coat_fixes = _audit_coat_rack(monkeypatch)
    conflict_issues, conflict_fixes = _audit_title_conflict(monkeypatch)
    remap = next(issue for issue in coat_issues if issue["type"] == "category_remap_pending_review")
    conflict = next(issue for issue in conflict_issues if issue["type"] == "title_source_conflict")
    assert remap["severity"] == "MEDIUM"
    assert remap["report_only"] is True
    assert conflict["severity"] == "LOW"
    assert conflict["report_only"] is True
    assert "categoryId" not in coat_fixes
    assert "categoryName" not in coat_fixes
    assert "categoryId" not in conflict_fixes

    from listing_qc import fix_key_matches_issue

    for key in list(coat_fixes) + list(conflict_fixes):
        assert not fix_key_matches_issue(str(key), "category_remap_pending_review")
        assert not fix_key_matches_issue(str(key), "title_source_conflict")

    db_path = project_root / "ebay_collection.db"
    rows = [
        {
            "sku": W5368_SKU,
            "title": COAT_RACK_TITLE,
            "listing_id": W5368_LISTING,
            "optimization": _pending_opt(W5368_LISTING),
        },
        {
            "sku": "TITLE-CONFLICT",
            "listing_id": "222",
            "optimization": _pending_opt("222", category_id="38208"),
        },
    ]
    _insert_published(db_path, rows)
    before = _optimization_by_sku(db_path)
    calls = _install_live_stubs(
        monkeypatch,
        {W5368_SKU: [remap], "TITLE-CONFLICT": [conflict]},
    )
    summary = _run(auto_fix=True)

    _assert_reported(summary, W5368_SKU, "MEDIUM")
    _assert_reported(summary, "TITLE-CONFLICT", "LOW")
    assert summary["critical"] == 0, summary
    assert summary["fixed"] == 0, summary
    assert calls["fix"] == [], calls
    assert calls["persist"] == [], calls
    assert _optimization_by_sku(db_path) == before


def test_edge_transport_nonstandard_severity(monkeypatch, project_root):
    db_path = project_root / "ebay_collection.db"
    _insert_published(
        db_path,
        [
            {
                "sku": "TRANSPORT-WARN",
                "listing_id": "900",
                "optimization": _pending_opt("900"),
            }
        ],
    )
    _install_live_stubs(
        monkeypatch,
        {
            "TRANSPORT-WARN": [
                {
                    "type": "live_fetch_failed",
                    "severity": "WARNING",
                    "detail": "transport severity was not canonical",
                }
            ]
        },
    )
    summary = _run(auto_fix=False)

    assert "error" not in summary, summary
    assert summary["issues_found"] == 0, summary
    assert summary["transport_failures"] == 1, summary
    assert summary["top_transport_failures"][0]["sku"] == "TRANSPORT-WARN"
    assert summary["top_transport_failures"][0]["max_severity"] == "MEDIUM", summary
    assert summary["critical"] == 0, summary


def test_edge_missing_none_lowercase_and_garbage(monkeypatch, project_root):
    cases = {
        "SEV-MISSING": ({"type": "wrong_dimension", "detail": "no severity"}, "LOW"),
        "SEV-NONE": ({"type": "wrong_dimension", "severity": None, "detail": "none"}, "LOW"),
        "SEV-HIGH": ({"type": "wrong_dimension", "severity": "high", "detail": "lower"}, "HIGH"),
        "SEV-FOO": ({"type": "wrong_dimension", "severity": "FOO", "detail": "garbage"}, "LOW"),
    }
    rows = []
    issues = {}
    for index, (sku, (issue, _expected)) in enumerate(cases.items(), start=1):
        listing_id = str(1000 + index)
        rows.append(
            {
                "sku": sku,
                "listing_id": listing_id,
                "optimization": _pending_opt(listing_id),
            }
        )
        issues[sku] = [issue]
    _insert_published(project_root / "ebay_collection.db", rows)
    _install_live_stubs(monkeypatch, issues)
    summary = _run(auto_fix=False)

    assert "error" not in summary, summary
    assert summary["critical"] == 0, summary
    for sku, (_issue, expected) in cases.items():
        _assert_reported(summary, sku, expected)


def test_edge_mixed_warning_and_critical(monkeypatch, project_root):
    sku = "MIXED-CRIT"
    _insert_published(
        project_root / "ebay_collection.db",
        [{"sku": sku, "listing_id": "1100", "optimization": _pending_opt("1100")}],
    )
    calls = _install_live_stubs(
        monkeypatch,
        {
            sku: [
                {"type": "wrong_dimension", "severity": "WARNING", "detail": "non-standard"},
                {"type": "description_raw_source_dump", "severity": "CRITICAL", "detail": "raw"},
            ]
        },
    )
    summary = _run(auto_fix=False)

    _assert_reported(summary, sku, "CRITICAL")
    assert summary["issues_found"] == 1, summary
    assert summary["critical"] == 1, summary
    assert calls["persist"] == [(sku, "dirty")], calls
    assert calls["fix"] == [], calls


def test_edge_canonical_only_matches_legacy(monkeypatch, project_root):
    rows_spec = {
        "CANON-CRIT": [{"type": "description_raw_source_dump", "severity": "CRITICAL", "detail": "raw"}],
        "CANON-MIX": [
            {"type": "missing_video", "severity": "HIGH", "detail": "video"},
            {"type": "incomplete_title", "severity": "MEDIUM", "detail": "title"},
            {"type": "title_cleanup", "severity": "LOW", "detail": "cleanup"},
        ],
    }
    rows = []
    for index, (sku, issues) in enumerate(rows_spec.items(), start=1):
        listing_id = str(1200 + index)
        rows.append({"sku": sku, "listing_id": listing_id, "optimization": _pending_opt(listing_id)})
    _insert_published(project_root / "ebay_collection.db", rows)
    calls = _install_live_stubs(monkeypatch, rows_spec)
    summary = _run(auto_fix=False)

    assert "error" not in summary, summary
    assert summary["issues_found"] == 2, summary
    assert summary["critical"] == 1, summary
    for sku, issues in rows_spec.items():
        legacy = _legacy_max_severity(issues)
        _assert_reported(summary, sku, legacy)
        assert legacy == _SEVERITY_ORDER[
            min(_SEVERITY_ORDER.index(normalize_severity(issue["severity"])) for issue in issues)
        ]
    assert set(calls["persist"]) == {("CANON-CRIT", "dirty"), ("CANON-MIX", "dirty")}, calls
    assert calls["fix"] == [], calls


class _OfflineMatcher:
    def is_category_plausible_for_text(self, *_args, **_kwargs):
        return True

    def canonicalize_category(self, *_args, **_kwargs):
        return None, None

    def get_category_and_aspects(self, *_args, **_kwargs):
        raise AssertionError("API suggest must not invent a leaf")

    def _get_category_aspects(self, _category_id):
        raise RuntimeError("offline taxonomy")


def _audit_quiet(monkeypatch, sku: str, giga_title: str, opt: dict, description: str):
    from scripts import audit_fix_active_listings

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _OfflineMatcher())
    monkeypatch.setattr(
        audit_fix_active_listings,
        "get_fact_sheet_conn",
        lambda: sqlite3.connect(":memory:"),
    )
    monkeypatch.setenv("AUDIT_SEMANTIC_FACT_SHEET", "0")
    return audit_fix_active_listings.audit_single_product(
        sku,
        giga_title,
        "{}",
        "{}",
        json.dumps(opt),
        description,
    )


_QUIET_DESCRIPTION = (
    "<div>AQUAVERVE KEY FEATURES<ul><li>Solid wood frame</li>"
    "<li>Assembly required: Yes</li></ul>"
    "Ships from US Warehouse</div>"
)


def _audit_coat_rack(monkeypatch):
    opt = {
        "title": COAT_RACK_TITLE,
        "categoryId": "103430",
        "categoryName": "Armoires & Wardrobes",
        "aspects": {"Brand": ["Unbranded"]},
        "description": _QUIET_DESCRIPTION,
    }
    return _audit_quiet(
        monkeypatch,
        W5368_SKU,
        COAT_RACK_TITLE,
        opt,
        "Solid wood rolling coat rack. Assembly required: Yes.",
    )


def _audit_title_conflict(monkeypatch):
    opt = {
        "title": "Outdoor Chair Mid-Century Modern Dark Green Chenille Rectangular Wood Frame",
        "categoryId": "38208",
        "aspects": {"Brand": ["Unbranded"]},
        "description": _QUIET_DESCRIPTION,
    }
    return _audit_quiet(
        monkeypatch,
        "W3835P484082",
        "Mid-Century Modern Accent Chair with Solid Wood Frame, Upholstered Chenille Living Room Arm Chairs",
        opt,
        "Accent chair. Assembly required: Yes.",
    )


def test_coat_rack_audit_emits_only_canonical_severities(monkeypatch):
    issues, fixes = _audit_coat_rack(monkeypatch)
    assert issues, "coat rack row must surface the pending remap"
    offenders = [issue for issue in issues if issue.get("severity") not in CANONICAL]
    assert offenders == [], offenders
    remap = [issue for issue in issues if issue.get("type") == "category_remap_pending_review"]
    assert len(remap) == 1, issues
    assert remap[0]["severity"] == "MEDIUM"
    assert remap[0]["report_only"] is True
    assert remap[0]["current"] == "103430"
    assert remap[0]["expected"] == "22656"
    assert "coat_rack_head" in str(remap[0].get("detail"))
    assert "categoryId" not in fixes
    assert "categoryName" not in fixes

    conflict_issues, conflict_fixes = _audit_title_conflict(monkeypatch)
    conflict_offenders = [
        issue for issue in conflict_issues if issue.get("severity") not in CANONICAL
    ]
    assert conflict_offenders == [], conflict_offenders
    conflict = [issue for issue in conflict_issues if issue.get("type") == "title_source_conflict"]
    assert len(conflict) == 1, conflict_issues
    assert conflict[0]["severity"] == "LOW"
    assert conflict[0]["report_only"] is True
    assert "categoryId" not in conflict_fixes


def test_gate_flag_emitter_is_canonical_and_report_only(monkeypatch):
    from scripts import audit_fix_active_listings

    opt = {
        "title": "Solid Wood Nightstand",
        "categoryId": "38199",
        "categoryApproved": True,
        "categoryApprovedId": "99999",
        "aspects": {"Brand": ["Unbranded"]},
        "description": _QUIET_DESCRIPTION,
    }
    issues, _fixes = _audit_quiet(
        monkeypatch,
        "PIN-1",
        "Solid Wood Nightstand",
        opt,
        "Nightstand. Assembly required: Yes.",
    )
    flags = [issue for issue in issues if issue.get("type") == "pin_mismatch"]
    assert len(flags) == 1, issues
    assert flags[0]["severity"] == "LOW"
    assert flags[0]["report_only"] is True
    assert flags[0]["severity"] in CANONICAL

    captured = {}

    def _fake_flags(_stored, *, optimization=None):
        captured["optimization"] = optimization
        return [NEEDS_MANUAL_MOTORS_LEAF_REVIEW]

    monkeypatch.setattr(audit_fix_active_listings, "category_gate_report_flags", _fake_flags)
    flagged, _fixes = _audit_quiet(
        monkeypatch,
        "GATE-1",
        "Solid Wood Nightstand",
        opt,
        "Nightstand. Assembly required: Yes.",
    )
    motors = [issue for issue in flagged if issue.get("type") == NEEDS_MANUAL_MOTORS_LEAF_REVIEW]
    assert len(motors) == 1, flagged
    assert motors[0]["severity"] == "LOW"
    assert motors[0]["report_only"] is True


def test_count_unverifiable_stays_report_only_after_normalization(monkeypatch, project_root):
    """Fact-sheet INFO flows into the audit issues list and must not write."""
    from scripts import audit_fix_active_listings

    def _fake_fact_sheet(**_kwargs):
        return {
            "status": "violations",
            "violations": [
                {
                    "claim_type": "count_unverifiable",
                    "claim_text": "3 shelves (source: 6)",
                    "severity": "INFO",
                    "source_evidence": "6",
                }
            ],
        }

    monkeypatch.setattr(
        "src.utils.listing_fact_sheet.check_fact_sheet_violations",
        _fake_fact_sheet,
    )
    issues, fixes = _audit_quiet(
        monkeypatch,
        "COUNT-1",
        "Solid Wood Nightstand",
        {
            "title": "Solid Wood Nightstand",
            "categoryId": "38199",
            "aspects": {"Brand": ["Unbranded"]},
            "description": _QUIET_DESCRIPTION,
        },
        "Nightstand. Assembly required: Yes.",
    )
    unverifiable = [issue for issue in issues if issue.get("type") == "count_unverifiable"]
    assert len(unverifiable) == 1, issues
    assert unverifiable[0]["severity"] == "LOW"
    assert unverifiable[0]["report_only"] is True
    assert unverifiable[0]["severity"] in CANONICAL

    db_path = project_root / "ebay_collection.db"
    _insert_published(
        db_path,
        [{"sku": "COUNT-1", "listing_id": "1300", "optimization": _pending_opt("1300", category_id="38199")}],
    )
    before = _optimization_by_sku(db_path)
    calls = _install_live_stubs(
        monkeypatch,
        {"COUNT-1": unverifiable},
        fixes_by_sku={"COUNT-1": {"__semantic_rebuild_from_source__": True}},
    )
    summary = _run(auto_fix=True)

    _assert_reported(summary, "COUNT-1", "LOW")
    assert summary["critical"] == 0, summary
    assert summary["fixed"] == 0, summary
    assert calls["fix"] == [], calls
    assert calls["persist"] == [], calls
    assert _optimization_by_sku(db_path) == before
