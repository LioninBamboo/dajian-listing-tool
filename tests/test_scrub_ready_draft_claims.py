"""READY-draft unsupported-claim scrub: table rows, checklist, extra edges."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest

from src.utils.ready_draft_claim_scrub import (
    MULTIPLE_MATCH_POLICY,
    apply_claim_to_candidate,
    apply_scrub_to_row,
    collect_flagged_claims,
    connect_collection_db,
    format_diff_lines,
    is_live_row,
    is_ready_unpublished,
    normalize_once,
    retry_locked,
    row_to_candidate_source,
    scrub_candidate,
    skip_solid_wood_fix,
    terms_equivalent,
    whole_value_match,
    FlaggedClaim,
)

def _profile_store_arg() -> str:
    from src.utils.store_profile import get_store_profile

    return str(getattr(get_store_profile(), "brand_name", "") or "AquaVerve")


def _mismatch_store_arg() -> str:
    brand = _profile_store_arg().casefold()
    if brand == "grovepop":
        return "main"
    return "gp"


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "scrub_ready_draft_claims.py"

GIGA_IMAGE = (
    "https://img.gigacloud.com/w331.jpg?x-cc=keep&x-cu=keep&x-ct=keep&x-cs=keep"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("scrub_ready_draft_claims", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cand(**overrides):
    payload = {
        "title": "Indoor Chair",
        "description": "<p>Indoor chair.</p>",
        "aspects": {"Brand": ["AquaVerve"]},
    }
    payload.update(overrides)
    return payload


def _src(**overrides):
    payload = {
        "title": "Indoor Chair",
        "description": "Indoor chair.",
        "attributes": {},
        "specs": {},
    }
    payload.update(overrides)
    return payload


def _types(claims):
    return {item.claim_type for item in claims}


def _joined(candidate):
    aspects = candidate.get("aspects") or {}
    aspect_text = " ".join(
        " ".join(str(v) for v in (val if isinstance(val, (list, tuple)) else [val]))
        for val in aspects.values()
    )
    return f"{candidate.get('title') or ''} {candidate.get('description') or ''} {aspect_text}"


# ── detector reuse ──────────────────────────────────────────────────────────


def test_collect_flagged_claims_reuses_existing_detectors_not_a_parallel_engine():
    source = Path(ROOT / "src" / "utils" / "ready_draft_claim_scrub.py").read_text(encoding="utf-8")
    assert "detect_claim_violations" in source
    assert "validate_listing_quality" in source
    assert "motors_deterministic_fact_violations" in source
    assert "build_source_constraints" in source
    flagged = collect_flagged_claims(
        _cand(title="Soft-close 150 lb cabinet", description="soft-close, 150 lb capacity"),
        _src(description="Cabinet. No other claims."),
    )
    types = _types(flagged)
    assert any("soft" in (c.claim_text + c.claim_type) for c in flagged)
    assert any(c.claim_type == "semantic_capacity" for c in flagged)
    assert "quality_gate" in {c.detector for c in flagged} or "claim_diff" in {c.detector for c in flagged}
    assert types


# ── owner table rows ────────────────────────────────────────────────────────


def test_table_w331p511603_removes_unsourced_45lb():
    cand = _cand(
        title="Massage Chair supports up to 45 lb Black",
        description="<li>Supports up to 45 lb for daily use.</li><li>Heated seat.</li>",
    )
    source = _src(title="Massage Chair", description="Heated massage chair. No load figure.")
    before = collect_flagged_claims(cand, source)
    assert any("45" in c.claim_text for c in before)
    out, result = scrub_candidate(cand, source, sku="W331P511603")
    text = _joined(out).lower()
    assert "45" not in text
    assert "supports up to" not in text
    assert "heated" in text
    assert result.status == "cleaned"
    assert any(d.action == "remove" for d in result.diffs)


def test_table_w331p511605_removes_unsourced_35lb():
    cand = _cand(title="Lounge Chair up to 35 lb", description="<p>Rated up to 35 lb.</p>")
    source = _src(description="Lounge chair.")
    out, result = scrub_candidate(cand, source, sku="W331P511605")
    assert "35" not in _joined(out).lower()
    assert result.status == "cleaned"


def test_table_w5568p534216_removes_250lb_keeps_supported_paraphrase():
    cand = _cand(
        title="Armchair 250 lb capacity with arm rest",
        description="<p>250 lb capacity. Upholstered arm rest.</p>",
        aspects={"Features": ["Upholstered", "Arm Rest"]},
    )
    source = _src(
        title="Upholstered armchair with arm rest",
        description="Upholstered armchair. Padded arm rest. No load figure.",
    )
    out, result = scrub_candidate(cand, source, sku="W5568P534216")
    text = _joined(out).lower()
    assert "250" not in text
    assert "upholstered" in text
    assert "arm rest" in text
    assert result.status == "cleaned"


def test_table_n707p545294w_removes_soft_close_and_150lb():
    cand = _cand(
        title="Soft-close Nightstand 150 lb",
        description="<li>Soft-close drawers. 150 lb capacity.</li>",
        aspects={"Features": ["Soft-Close"]},
    )
    source = _src(description="Nightstand with two drawers. Oak veneer look.")
    before = collect_flagged_claims(cand, source)
    assert any("soft" in (c.claim_text + c.claim_type).lower() for c in before)
    out, result = scrub_candidate(cand, source, sku="N707P545294W")
    text = _joined(out).lower()
    assert "soft-close" not in text and "soft close" not in text
    assert "150" not in text
    assert result.status == "cleaned"


def test_table_w3224p556674_removes_350lb_and_no_electricity_keeps_heat():
    cand = _cand(
        title="Massage Heat Chair supports up to 350 lbs",
        description=(
            "<p>Supports up to 350 lbs; no electricity or batteries required. "
            "Massage and heat functions.</p>"
        ),
    )
    source = _src(
        title="Massage Heat Chair",
        description="Massage chair with heating function. Plug-in heated seat.",
    )
    before = collect_flagged_claims(cand, source)
    assert any("350" in c.claim_text for c in before)
    assert any("electricity" in (c.claim_text + c.claim_type).lower() or "no_electricity" in (c.claim_text + c.claim_type) for c in before)
    out, result = scrub_candidate(cand, source, sku="W3224P556674")
    text = _joined(out).lower()
    assert "350" not in text
    assert "no electricity" not in text
    assert "massage" in text
    assert "heat" in text
    assert result.status == "cleaned"


def test_table_w527p510517_removes_no_electricity_on_heated_product():
    cand = _cand(
        title="Heated Throw no electricity required",
        description="<p>Heated throw, no electricity required.</p>",
    )
    source = _src(title="Heated Throw", description="Heated electric throw blanket.")
    out, _result = scrub_candidate(cand, source, sku="W527P510517")
    text = _joined(out).lower()
    assert "no electricity" not in text
    assert "heated" in text


def test_table_w3590p576948_source_corrects_6_person_to_4_6():
    cand = _cand(
        title="6 person camping tent",
        description="<p>Spacious 6 person shelter.</p>",
        aspects={"Seating Capacity": ["6 Person"]},
    )
    source = _src(title="4-6 person tent", description="Occupancy 4-6 person tent.")
    before = collect_flagged_claims(cand, source)
    assert any(c.claim_type == "semantic_capacity" for c in before)
    out, result = scrub_candidate(cand, source, sku="W3590P576948")
    text = _joined(out).lower()
    assert result.status == "cleaned"
    assert "6 person" not in text.replace("4-6 person", "").replace("4-6", "")
    assert "4-6" in text or "6 person" not in text
    if any(d.action == "source_correct" for d in result.diffs):
        assert "4-6" in text
    else:
        assert all(d.action == "remove" for d in result.diffs)


def test_table_w5870p523990_source_corrects_fabric_and_seat_count():
    cand = _cand(
        title="Fabric 1 seat bench",
        description="<p>Fabric bench, 1 seat.</p>",
        aspects={"Material": ["Fabric"], "Seating Capacity": ["1 seat"]},
    )
    source = _src(
        title="Microsuede oak bench",
        description="Microsuede upholstery, oak frame, 2 seats.",
        attributes={"Material": "Microsuede", "Frame Material": "Oak"},
    )
    out, result = scrub_candidate(cand, source, sku="W5870P523990")
    text = _joined(out).lower()
    assert "microsuede" in text
    assert "1 seat" not in text
    assert "2 seat" in text
    assert result.status == "cleaned"


def test_table_w3615p530680_removes_no_assembly_does_not_invent():
    cand = _cand(
        title="Storage Cabinet no assembly",
        description="<p>No assembly required. Ready to use.</p>",
        aspects={"Assembly Required": ["No"]},
    )
    source = _src(
        title="Storage Cabinet",
        description="Flat-pack cabinet. Easy assembly. Ships as 2 separate cartons.",
        attributes={"Assembly Required": "Yes"},
        specs={
            "Package Length (in.)": "8",
            "Package Width (in.)": "20",
            "Package Height (in.)": "30",
            "Assembled Length (in.)": "30",
            "Assembled Width (in.)": "16",
            "Assembled Height (in.)": "70",
        },
    )
    before = collect_flagged_claims(cand, source)
    assert any(c.claim_type.startswith("assembly_") for c in before)
    out, result = scrub_candidate(cand, source, sku="W3615P530680")
    text = _joined(out).lower()
    assert "no assembly" not in text
    # Never add an invented "assembly required: yes" sentence.
    assert "assembly is required before use" not in text
    assert result.status == "cleaned"


def test_table_w2105p595057_negative_unchanged():
    cand = _cand(
        title="Oak Side Table 40 Inch",
        description="<p>Oak side table with storage shelf.</p>",
        aspects={"Material": ["Oak"], "Features": ["With Storage"]},
    )
    source = _src(
        title="Oak Side Table 40 Inch",
        description="Oak side table with storage shelf.",
        attributes={"Material": "Oak"},
    )
    out, result = scrub_candidate(cand, source, sku="W2105P595057")
    assert result.status == "unchanged"
    assert out["title"] == cand["title"]
    assert out["description"] == cand["description"]
    assert result.diffs == []


def test_table_gp_w2500p623899_keeps_cushioned_paraphrase():
    cand = _cand(
        title="Cushioned patio chair",
        description="<p>Cushioned seat for outdoor lounging.</p>",
    )
    source = _src(
        title="Patio chair",
        description="Full cushion seat. Outdoor patio chair.",
    )
    out, result = scrub_candidate(cand, source, sku="W2500P623899")
    assert "cushioned" in _joined(out).lower()
    assert result.status == "unchanged"


# ── solid wood vs engineered ────────────────────────────────────────────────


def test_solid_wood_rubberwood_not_scrubbed_mdf_is_removed():
    solid = FlaggedClaim(
        claim_type="semantic_material",
        claim_text="solid wood",
        source_evidence="NOT_FOUND",
        location="title",
        detector="motors_fact",
        severity="CRITICAL",
    )
    assert skip_solid_wood_fix(solid, "rubberwood dining table") is True
    assert skip_solid_wood_fix(solid, "eucalyptus frame") is True
    assert skip_solid_wood_fix(solid, "acacia wood") is True
    assert skip_solid_wood_fix(solid, "MDF particle board desk") is False

    rubber = _src(description="Rubberwood dining table.", attributes={"Material": "Rubberwood"})
    live_ok = _cand(title="Solid wood dining table", description="<p>Solid wood top.</p>")
    flags = collect_flagged_claims(live_ok, rubber)
    solid_flags = [c for c in flags if "solid wood" in c.claim_text.lower()]
    if solid_flags:
        assert all(skip_solid_wood_fix(c, "rubberwood dining table") for c in solid_flags)
    out, result = scrub_candidate(live_ok, rubber, sku="WOOD-RUBBER")
    assert "solid wood" in _joined(out).lower()

    euc = _src(description="Eucalyptus frame.", attributes={"Main Material": "Eucalyptus"})
    out_e, _ = scrub_candidate(
        _cand(title="Solid wood bench", description="Solid wood."),
        euc,
        sku="WOOD-EUC",
    )
    assert "solid wood" in _joined(out_e).lower()

    acacia = _src(description="Acacia wood.", attributes={"Material": "Acacia"})
    out_a, _ = scrub_candidate(
        _cand(title="Solid wood stool", description="Solid wood."),
        acacia,
        sku="WOOD-ACA",
    )
    assert "solid wood" in _joined(out_a).lower()

    mdf = _src(description="MDF particle board desk.", attributes={"Material": "MDF"})
    out_m, result_m = scrub_candidate(
        _cand(title="Solid wood desk", description="<p>Solid wood construction.</p>"),
        mdf,
        sku="WOOD-MDF",
    )
    assert "solid wood" not in _joined(out_m).lower()
    assert result_m.status == "cleaned"


# ── boundary checklist ──────────────────────────────────────────────────────


def test_whole_value_equality_not_substring():
    assert whole_value_match("table", "stable platform") is False
    assert whole_value_match("table", "oak table") is True
    assert whole_value_match("Leather", "PU Leather sofa") is False
    assert whole_value_match("Leather", "Leather sofa") is True
    assert whole_value_match("47 chairs", "47 Inch Chairs") is False
    assert whole_value_match("2 pack", "2-Piece dining set") is False
    assert whole_value_match("2 pack", "2 Pack towels") is True


def test_normalization_once_same_on_both_sides():
    left = "Black ＆ White 　Chairs"
    right = "black and white chairs"
    assert normalize_once(left).casefold() == normalize_once(right).casefold()
    assert terms_equivalent("Black & White Chairs", "black and white chair")
    assert terms_equivalent("２　Pack", "2 pack")
    assert normalize_once("a\r\nb") == "a b"


def test_empty_none_missing_fields_never_crash():
    out, result = scrub_candidate(None, None, sku="EMPTY")
    assert result.status == "unchanged"
    assert isinstance(out, dict)
    assert result.diffs == []
    out2, result2 = scrub_candidate({"title": None, "description": None, "aspects": None}, {}, sku="NONE")
    assert isinstance(out2, dict)
    assert result2.status == "unchanged"
    flags = collect_flagged_claims({}, None)
    assert flags == []


def test_multiple_matches_first_then_all_and_policy_stated():
    assert "left-to-right" in MULTIPLE_MATCH_POLICY
    cand = _cand(
        title="Chair supports up to 45 lb and extra 45 lb rating",
        description="<p>45 lb. Also 45 lb hook.</p>",
    )
    source = _src(description="Chair.")
    out, result = scrub_candidate(cand, source, sku="MULTI")
    assert "45" not in _joined(out)
    assert result.diffs


def test_unknown_value_never_mapped_to_nearest_guess():
    cand = _cand(title="Fabric bench", description="<p>Fabric bench.</p>")
    source = _src(description="Oak frame bench. No fabric named.", attributes={"Material": "Oak"})
    out, _result = scrub_candidate(cand, source, sku="UNKNOWN-FABRIC")
    text = _joined(out).lower()
    assert "microsuede" not in text
    assert "chenille" not in text


def test_numbers_zero_decimal_range_fraction_missing_unit():
    # 0 lb is a real mention and is removed when unsourced.
    out0, _ = scrub_candidate(
        _cand(title="Hook 0 lb", description="<p>0 lb rating.</p>"),
        _src(description="Hook."),
        sku="NUM-0",
    )
    assert "0 lb" not in _joined(out0).lower()
    out_d, _ = scrub_candidate(
        _cand(title="Shelf 12.5 lb", description="12.5 lb"),
        _src(description="Shelf."),
        sku="NUM-DEC",
    )
    assert "12.5" not in _joined(out_d)
    # Range source-correct.
    out_r, result_r = scrub_candidate(
        _cand(title="30-40 lb bag", description="30-40 lb"),
        _src(description="Rated 20-25 lb."),
        sku="NUM-RANGE",
    )
    text_r = _joined(out_r).lower()
    assert "30-40" not in text_r
    assert result_r.status == "cleaned"
    # Fraction with no unit is not treated as a chair/person count.
    out_f, result_f = scrub_candidate(
        _cand(title="Board 1/2", description="<p>1/2 sheet.</p>"),
        _src(description="Board."),
        sku="NUM-FRAC",
    )
    assert "1/2" in _joined(out_f)
    assert result_f.status == "unchanged"
    # Missing unit: "47 Inch Chairs" is inches, not 47 chairs.
    out_u, result_u = scrub_candidate(
        _cand(title="47 Inch Chairs", description="<p>47 Inch Chairs set.</p>"),
        _src(description="47 Inch Chairs."),
        sku="NUM-INCH",
    )
    assert "47 Inch" in out_u["title"]
    assert result_u.status == "unchanged"


def test_empty_title_after_strip_is_needs_manual():
    cand = _cand(title="45 lb", description="<p>Oak frame. 45 lb hook.</p>")
    source = _src(description="Oak frame hook.")
    out, result = scrub_candidate(cand, source, sku="EMPTY-TITLE")
    assert result.status == "needs_manual"
    assert out["title"] == "45 lb"
    assert "45" not in (out.get("description") or "")


def test_idempotent_second_pass_makes_no_further_changes():
    cand = _cand(
        title="Soft-close 150 lb nightstand",
        description="<li>Soft-close. 150 lb capacity.</li>",
    )
    source = _src(description="Nightstand.")
    first, result1 = scrub_candidate(cand, source, sku="IDEM")
    second, result2 = scrub_candidate(first, source, sku="IDEM")
    assert result1.status == "cleaned"
    assert result2.status == "unchanged"
    assert second["title"] == first["title"]
    assert second["description"] == first["description"]


def test_scrub_does_not_strip_pu_leather_when_claim_is_leather():
    cand = _cand(
        title="Genuine Leather sofa with PU Leather trim",
        description="<p>Genuine leather upholstery on a PU Leather sofa.</p>",
        aspects={"Material": ["PU Leather"]},
    )
    source = _src(description="PU Leather sofa.", attributes={"Material": "PU Leather"})
    before = collect_flagged_claims(cand, source)
    leather_flags = [
        c for c in before
        if "leather" in f"{c.claim_text} {c.claim_type}".lower()
        and "pu" not in c.claim_text.lower()
    ]
    assert leather_flags, "must flag a Leather claim while PU Leather is present"
    out, result = scrub_candidate(cand, source, sku="PU-LEATHER")
    text = _joined(out)
    assert "PU Leather" in text
    assert "genuine leather" not in text.lower()
    claim = FlaggedClaim(
        claim_type="semantic_material",
        claim_text="Leather",
        source_evidence="NOT_FOUND",
        location="title",
        detector="motors_fact",
        severity="CRITICAL",
    )
    working = {
        "title": "Leather PU Leather sofa",
        "description": "<p>Leather. PU Leather sofa.</p>",
        "aspects": {"Material": ["PU Leather"]},
    }
    diffs, _reason = apply_claim_to_candidate(working, claim, "PU Leather sofa.", source)
    assert "PU Leather" in working["title"]
    assert "PU Leather" in working["description"]
    assert working["aspects"]["Material"] == ["PU Leather"]
    assert diffs
    assert working["title"].startswith("PU Leather") or "Leather PU Leather" not in working["title"]


def test_never_touch_giga_image_url_params():
    desc = f'<p>Supports up to 45 lb.</p><img src="{GIGA_IMAGE}"/>'
    cand = _cand(title="Chair supports up to 45 lb", description=desc, images=[GIGA_IMAGE])
    source = _src(description="Chair.", images=[GIGA_IMAGE])
    out, result = scrub_candidate(cand, source, sku="IMG")
    assert GIGA_IMAGE in (out.get("description") or "")
    assert out.get("images") == [GIGA_IMAGE]
    assert result.images_unchanged is True
    assert "x-cc=keep&x-cu=keep&x-ct=keep&x-cs=keep" in (out.get("description") or "")


# ── CLI / DB ────────────────────────────────────────────────────────────────


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            attributes TEXT,
            specs TEXT,
            images TEXT,
            videos TEXT,
            optimization TEXT,
            status TEXT,
            listing_id TEXT,
            logs TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return db


def _insert(db: Path, sku: str, status: str, opt: dict, *, listing_id: str = "", source_desc: str = "Chair.", extra: dict | None = None):
    extra = extra or {}
    conn = sqlite3.connect(db)
    conn.execute(
        """
        INSERT INTO collected_products
        (sku, title, description, attributes, specs, images, videos, optimization, status, listing_id, logs)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sku,
            extra.get("title", "Source Chair"),
            extra.get("description", source_desc),
            json.dumps(extra.get("attributes", {})),
            json.dumps(extra.get("specs", {})),
            json.dumps(extra.get("images", [GIGA_IMAGE])),
            json.dumps([]),
            json.dumps(opt),
            status,
            listing_id,
            json.dumps([]),
        ),
    )
    conn.commit()
    conn.close()


def test_cli_dry_run_writes_report_not_db(tmp_path):
    mod = _load_script()
    db = _make_db(tmp_path)
    opt = {
        "title": "Chair supports up to 45 lb",
        "description": f"<p>Supports up to 45 lb.</p><img src='{GIGA_IMAGE}'/>",
        "aspects": {"Brand": ["AquaVerve"]},
    }
    _insert(db, "W331P511603", "READY", opt)
    reports = tmp_path / "reports"
    code = mod.main(["--db", str(db), "--sku", "W331P511603", "--report-dir", str(reports)])
    assert code == 0
    conn = sqlite3.connect(db)
    stored = json.loads(conn.execute("SELECT optimization FROM collected_products WHERE sku='W331P511603'").fetchone()[0])
    conn.close()
    assert stored["title"] == opt["title"]
    files = list(reports.glob("scrub_ready_draft_claims_dry-run_*.json"))
    assert files
    report = json.loads(files[0].read_text(encoding="utf-8"))
    assert report["mode"] == "dry-run"
    assert report["results"][0]["sku"] == "W331P511603"
    assert report["results"][0]["diffs"]


def test_cli_apply_writes_ready_refuses_published_and_backs_up(tmp_path):
    mod = _load_script()
    db = _make_db(tmp_path)
    opt = {
        "title": "Chair supports up to 45 lb",
        "description": f"<p>Supports up to 45 lb.</p><img src='{GIGA_IMAGE}'/>",
        "aspects": {},
    }
    _insert(db, "W331P511603", "READY", opt)
    _insert(db, "LIVE-SKU", "PUBLISHED", dict(opt), listing_id="123456")
    reports = tmp_path / "reports"
    code = mod.main([
        "--db", str(db),
        "--sku", "W331P511603",
        "--sku", "LIVE-SKU",
        "--apply",
        "--report-dir", str(reports),
        "--store", _profile_store_arg(),
    ])
    assert code == 0
    conn = sqlite3.connect(db)
    rows = {sku: json.loads(optn) for sku, optn in conn.execute("SELECT sku, optimization FROM collected_products")}
    live_status = conn.execute("SELECT status FROM collected_products WHERE sku='LIVE-SKU'").fetchone()[0]
    conn.close()
    assert "45" not in rows["W331P511603"]["title"]
    assert GIGA_IMAGE in rows["W331P511603"]["description"]
    assert rows["LIVE-SKU"]["title"] == opt["title"]
    assert live_status == "PUBLISHED"
    backups = list(reports.glob("scrub_ready_draft_claims_backup_*.json"))
    assert backups
    report = json.loads(next(reports.glob("scrub_ready_draft_claims_apply_*.json")).read_text(encoding="utf-8"))
    statuses = {item["sku"]: item["status"] for item in report["results"]}
    assert statuses["LIVE-SKU"] == "refused_live"
    assert statuses["W331P511603"] == "cleaned"


def test_cli_apply_refuses_ready_with_listing_id(tmp_path):
    mod = _load_script()
    db = _make_db(tmp_path)
    _insert(
        db,
        "READY-LIVE",
        "READY",
        {"title": "Chair supports up to 45 lb", "description": "<p>45 lb</p>", "aspects": {}},
        listing_id="999",
    )
    reports = tmp_path / "reports"
    code = mod.main(["--db", str(db), "--sku", "READY-LIVE", "--apply", "--report-dir", str(reports)])
    assert code == 0
    conn = sqlite3.connect(db)
    title = json.loads(conn.execute("SELECT optimization FROM collected_products").fetchone()[0])["title"]
    conn.close()
    assert "45" in title


def test_cli_missing_db_is_degraded_exit_2(tmp_path):
    mod = _load_script()
    reports = tmp_path / "reports"
    missing = tmp_path / "missing.db"
    code = mod.main(["--db", str(missing), "--sku", "X", "--report-dir", str(reports)])
    assert code == 2
    assert not missing.exists()


def test_retry_locked_retries_then_raises():
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked after"):
        retry_locked(_boom, retries=3, base_delay=0.01)
    assert calls["n"] == 3


def test_is_live_and_ready_helpers():
    assert is_live_row("PUBLISHED", "") is True
    assert is_live_row("READY", "123") is True
    assert is_live_row("READY", "") is False
    assert is_ready_unpublished("READY", "") is True
    assert is_ready_unpublished("READY_TO_PUBLISH", None) is True
    assert is_ready_unpublished("READY", "1") is False
    assert is_ready_unpublished("PENDING", "") is False


def test_windows_path_quoting_and_utf8_helpers(tmp_path):
    mod = _load_script()
    spaced = tmp_path / "my reports"
    spaced.mkdir()
    db = _make_db(tmp_path)
    # Path with space is accepted by argparse type=Path.
    _insert(db, "W2105P595057", "READY", {
        "title": "Oak Side Table",
        "description": "<p>Oak side table.</p>",
        "aspects": {"Material": ["Oak"]},
    }, source_desc="Oak side table.", extra={"attributes": {"Material": "Oak"}})
    code = mod.main(["--db", str(db), "--sku", "W2105P595057", "--report-dir", str(spaced)])
    assert code == 0
    assert list(spaced.glob("*.json"))
    src = SCRIPT.read_text(encoding="utf-8")
    assert "force_utf8_stdio" in src
    assert 'encoding="utf-8"' in src
    assert "type=Path" in src


def test_script_never_calls_ebay_or_create_relist():
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in {"RelistFixedPriceItem", "AddFixedPriceItem"}
    assert "publish_offer" not in names
    assert "add_fixed_price_item_motors" not in names
    assert "publish_by_inventory_item_group" not in names
    assert "DATABASE_PATH" in source
    assert "ebay_tokens" not in "".join(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "never" not in node.value
    )
    lowered = source.lower()
    assert "authorization:" not in lowered
    assert "client_secret" not in lowered


def test_dry_run_diff_printer_includes_source_current_proposed():
    cand = _cand(title="Chair supports up to 45 lb", description="<p>45 lb</p>")
    source = _src(description="Chair. No load.")
    _out, result = scrub_candidate(cand, source, sku="W331P511603")
    result.row_status = "READY"
    lines = "\n".join(format_diff_lines(result, source))
    assert "W331P511603" in lines
    assert "source:" in lines
    assert "current:" in lines
    assert "proposed:" in lines


def test_row_to_candidate_handles_json_and_empty():
    row = {
        "optimization": json.dumps({"title": "A", "description": "B", "aspects": {"K": ["V"]}}),
        "title": "Src",
        "description": "Src desc",
        "attributes": "{}",
        "specs": None,
        "images": json.dumps([GIGA_IMAGE]),
        "videos": None,
    }
    cand, src = row_to_candidate_source(row)
    assert cand["title"] == "A"
    assert src["title"] == "Src"
    assert cand["images"] == [GIGA_IMAGE]


def test_cli_skus_file_crlf(tmp_path):
    mod = _load_script()
    db = _make_db(tmp_path)
    _insert(db, "W2105P595057", "READY", {
        "title": "Oak Side Table",
        "description": "<p>Oak side table.</p>",
        "aspects": {"Material": ["Oak"]},
    }, source_desc="Oak side table.", extra={"attributes": {"Material": "Oak"}})
    sku_file = tmp_path / "skus.txt"
    sku_file.write_bytes(b"W2105P595057\r\n# comment\r\n")
    reports = tmp_path / "reports"
    code = mod.main(["--db", str(db), "--skus-file", str(sku_file), "--report-dir", str(reports)])
    assert code == 0
    report = json.loads(next(reports.glob("*.json")).read_text(encoding="utf-8"))
    assert report["results"][0]["sku"] == "W2105P595057"


def test_connect_sets_busy_timeout(tmp_path):
    db = _make_db(tmp_path)
    conn = connect_collection_db(str(db))
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert int(timeout) >= 30000


def test_supported_weight_untouched_when_unsupported_load_removed():
    cand = _cand(
        title="Chair supports 300 lbs",
        description="<p>Supports 300 lbs. Net weight 30 lbs. Weighs 30 lbs.</p>",
        aspects={"Item Weight": ["30 lbs"], "Weight": ["30 lbs"]},
    )
    source = _src(
        title="Chair",
        description="Net weight 30 lbs. Weighs 30 lbs.",
        attributes={"Item Weight": "30 lbs"},
        specs={"Weight": "30 lbs"},
    )
    before = collect_flagged_claims(cand, source)
    assert any("300" in c.claim_text for c in before)
    assert not any(
        "30" in c.claim_text and "300" not in c.claim_text for c in before
    )
    out, result = scrub_candidate(cand, source, sku="WEIGHT-LOAD")
    desc = (out.get("description") or "").lower()
    assert "300" not in _joined(out).lower()
    assert "supports 300" not in desc
    assert "net weight 30 lbs" in desc
    assert "weighs 30 lbs" in desc
    assert "30" in str(out["aspects"].get("Item Weight") or "")
    assert "30" in str(out["aspects"].get("Weight") or "")
    assert result.status == "cleaned"
    first, _ = scrub_candidate(cand, source, sku="WEIGHT-LOAD")
    second, result2 = scrub_candidate(first, source, sku="WEIGHT-LOAD")
    assert result2.status == "unchanged"
    assert second["title"] == first["title"]
    assert second["description"] == first["description"]
    assert second["aspects"] == first["aspects"]


def test_linen_look_polyester_is_not_guessed_as_linen():
    claim = FlaggedClaim(
        claim_type="semantic_material",
        claim_text="Fabric",
        source_evidence="NOT_FOUND",
        location="title",
        detector="motors_fact",
        severity="CRITICAL",
    )
    sourced = _src(
        description="Upholstery in linen-look polyester.",
        attributes={"Material": "linen-look polyester"},
    )
    working = {
        "title": "Fabric bench",
        "description": "<p>Fabric bench.</p>",
        "aspects": {"Material": ["Fabric"]},
    }
    apply_claim_to_candidate(working, claim, "Upholstery in linen-look polyester.", sourced)
    text = _joined(working).lower()
    assert "linen" not in text
    assert "fabric" not in text

    guessed = _src(description="Upholstery in linen-look polyester.")
    working2 = {
        "title": "Fabric bench",
        "description": "<p>Fabric bench.</p>",
        "aspects": {"Material": ["Fabric"]},
    }
    apply_claim_to_candidate(working2, claim, "Upholstery in linen-look polyester.", guessed)
    text2 = _joined(working2).lower()
    assert "linen" not in text2
    assert "fabric" not in text2


def test_fabric_replaced_only_with_single_fabric_material():
    claim = FlaggedClaim(
        claim_type="semantic_material",
        claim_text="Fabric",
        source_evidence="NOT_FOUND",
        location="title",
        detector="motors_fact",
        severity="CRITICAL",
    )

    def _apply(material: str) -> str:
        working = {
            "title": "Fabric seat",
            "description": "<p>Fabric seat.</p>",
            "aspects": {"Material": ["Fabric"]},
        }
        source = _src(description="Source item.", attributes={"Material": material})
        apply_claim_to_candidate(working, claim, f"Material {material}", source)
        return _joined(working)

    metal = _apply("Metal")
    assert "metal" not in metal.lower()
    assert "fabric" not in metal.lower()

    wood = _apply("Solid Wood")
    assert "solid wood" not in wood.lower()
    assert "fabric" not in wood.lower()

    mixed = _apply("Polyester, Steel")
    assert "polyester, steel" not in mixed.lower()
    assert "steel" not in mixed.lower()
    assert "fabric" not in mixed.lower()

    poly = _apply("Polyester")
    assert "polyester" in poly.lower()
    assert "fabric" not in poly.lower()


def test_multiple_labelled_loads_are_removed_not_replaced():
    cand = _cand(
        title="Chair supports 300 lbs",
        description="<p>Supports 300 lbs.</p>",
    )
    source = _src(
        description="Seat weight capacity 250 lb. Shelf max load 20 lb.",
    )
    out, result = scrub_candidate(cand, source, sku="MULTI-LOAD")
    text = _joined(out).lower()
    assert "300" not in text
    assert "250" not in text
    assert "20 lb" not in text and "20 lbs" not in text
    assert result.status == "cleaned"
    assert any(d.action == "remove" for d in result.diffs)
    assert not any(d.action == "source_correct" for d in result.diffs)


def test_six_persons_not_sourced_by_sixteen_persons():
    cand = _cand(
        title="6 person canopy",
        description="<p>6 person shelter.</p>",
        aspects={"Seating Capacity": ["6 Person"]},
    )
    source = _src(title="16 persons canopy", description="Occupancy 16 persons.")
    before = collect_flagged_claims(cand, source)
    cap_flags = [c for c in before if c.claim_type == "semantic_capacity"]
    assert cap_flags
    assert any("6" in c.claim_text for c in cap_flags)
    out, result = scrub_candidate(cand, source, sku="SIX-VS-SIXTEEN")
    text = _joined(out).lower()
    assert "6 person" not in text.replace("16 person", "")
    assert result.status == "cleaned"


def test_out_of_range_occupancy_is_source_corrected():
    cand = _cand(title="8 person tent", description="<p>8 person shelter.</p>")
    source = _src(title="4-6 person tent", description="Occupancy 4-6 person.")
    before = collect_flagged_claims(cand, source)
    assert any(c.claim_type == "semantic_capacity" for c in before)
    out, result = scrub_candidate(cand, source, sku="OCC-OUT")
    text = _joined(out).lower()
    assert "8 person" not in text
    assert result.status == "cleaned"


def test_cli_missing_sku_exits_nonzero(tmp_path):
    mod = _load_script()
    db = _make_db(tmp_path)
    reports = tmp_path / "reports"
    code = mod.main(["--db", str(db), "--sku", "MISSING-SKU", "--report-dir", str(reports)])
    assert code != 0
    report = json.loads(next(reports.glob("*.json")).read_text(encoding="utf-8"))
    assert report["results"][0]["status"] == "missing"
    assert "not found" in str(report["results"][0].get("reason") or "").lower()


def test_cli_store_mismatch_is_rejected(tmp_path):
    mod = _load_script()
    db = _make_db(tmp_path)
    reports = tmp_path / "reports"
    code = mod.main(["--db", str(db), "--store", _mismatch_store_arg(), "--report-dir", str(reports)])
    assert code != 0
    assert list(reports.glob("*.json")) == []


def test_cli_apply_skips_needs_manual_with_partial_diffs(tmp_path):
    mod = _load_script()
    db = _make_db(tmp_path)
    opt = {
        "title": "45 lb",
        "description": "<p>Oak frame. 45 lb hook.</p>",
        "aspects": {},
    }
    _insert(db, "EMPTY-TITLE", "READY", opt, source_desc="Oak frame hook.")
    reports = tmp_path / "reports"
    code = mod.main([
        "--db", str(db),
        "--sku", "EMPTY-TITLE",
        "--apply",
        "--report-dir", str(reports),
        "--store", _profile_store_arg(),
    ])
    assert code == 0
    conn = sqlite3.connect(db)
    stored = json.loads(conn.execute("SELECT optimization FROM collected_products").fetchone()[0])
    conn.close()
    assert stored["title"] == "45 lb"
    report = json.loads(next(reports.glob("scrub_ready_draft_claims_apply_*.json")).read_text(encoding="utf-8"))
    assert report["results"][0]["status"] == "needs_manual"
    assert report.get("written", 0) == 0


def test_cli_missing_db_does_not_create_file(tmp_path):
    mod = _load_script()
    missing = tmp_path / "missing.db"
    reports = tmp_path / "reports"
    code = mod.main(["--db", str(missing), "--sku", "X", "--report-dir", str(reports)])
    assert code == 2
    assert not missing.exists()


def test_dry_run_opens_db_readonly(tmp_path):
    db = _make_db(tmp_path)
    conn = connect_collection_db(str(db), readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE should_fail (id INTEGER)")
    conn.close()


def test_apply_refused_write_uses_rowcount(tmp_path):
    db = _make_db(tmp_path)
    _insert(
        db,
        "X",
        "READY",
        {"title": "Chair", "description": "<p>Chair.</p>", "aspects": {}},
    )
    conn = connect_collection_db(str(db))
    row = conn.execute("SELECT * FROM collected_products").fetchone()
    conn.execute("UPDATE collected_products SET status='PUBLISHED' WHERE sku='X'")
    conn.commit()
    with pytest.raises(PermissionError, match="refused write"):
        apply_scrub_to_row(
            conn,
            row,
            {"title": "Edited", "description": "<p>Chair.</p>", "aspects": {}},
        )
    stored = json.loads(
        conn.execute("SELECT optimization FROM collected_products WHERE sku='X'").fetchone()[0]
    )
    conn.close()
    assert stored["title"] == "Chair"
