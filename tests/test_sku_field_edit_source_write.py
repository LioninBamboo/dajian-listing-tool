"""Source-checked --set-title / --set-aspect for local drafts only."""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.services import sku_field_edit as sfe
from src.utils import source_checked_draft as scd
from tests.test_sku_field_edit import make_ctx, run

GIGA_IMAGE = (
    "https://img.gigab2b.com/photo/W2615P429613.jpg?x-cc=1&x-cu=2&x-ct=3&x-cs=4"
)

CABINET_SOURCE_TITLE = (
    'TIA 12" Bathroom Wall Mounted Narrow Storage Cabinet with 2 Open Shelves'
)
CABINET_SOURCE_DESC = "Complemented by a sintered stone countertop"
CABINET_ACCEPT_TITLE = (
    'Wall Mounted Narrow Bathroom Cabinet 12" with 2 Open Shelves, Sintered Stone Top'
)
CABINET_REFUSE_TITLE = 'Floor Standing Narrow Bathroom Cabinet 12"'

CHAIR_SOURCE_TITLE = (
    "Ergonomic Design Chair Lumbar Support Chair,Model Foam Frame with "
    "Soft Velvet Upholstered Chair (Beige)"
)
CHAIR_ACCEPT_TITLE = (
    "Ergonomic Lumbar Support Chair, Soft Velvet Upholstered, EPP Foam Frame, Beige"
)
CHAIR_REFUSE_TITLE = "Velvet Barrel Armchair with Lumbar Support Beige"


@pytest.fixture(autouse=True)
def _no_llm_fact_sheet(monkeypatch):
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.setenv("AUDIT_SEMANTIC_FACT_SHEET", "0")
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")

    def fake_fact_sheet(*_args, **_kwargs):
        return {"status": "pass", "violations": []}

    monkeypatch.setattr(scd, "check_fact_sheet_violations", fake_fact_sheet)
    return fake_fact_sheet


def draft_row(
    sku: str,
    *,
    source_title: str,
    source_desc: str = "",
    attributes: dict | None = None,
    specs: dict | None = None,
    status: str = "READY",
    listing_id: str | None = None,
    opt_title: str | None = None,
    opt_desc: str | None = None,
    aspects: dict | None = None,
    images: list[str] | None = None,
) -> dict:
    return {
        "sku": sku,
        "title": source_title,
        "price": 10.0,
        "stock": 5,
        "status": status,
        "listing_id": listing_id,
        "shipping": 0.0,
        "url": f"https://example.test/{sku}",
        "images": list(images if images is not None else [GIGA_IMAGE]),
        "videos": [],
        "description": source_desc,
        "attributes": attributes or {},
        "specs": specs or {},
        "optimization": {
            "title": opt_title or "Wrong Floor Standing Draft Title Extra Words Here",
            "description": source_desc if opt_desc is None else opt_desc,
            "aspects": aspects or {"Brand": ["AquaVerve"]},
            "categoryId": "20487",
            "categoryName": "Cabinets & Cupboards",
        },
        "logs": [],
    }


def cabinet_row(**kwargs) -> dict:
    return draft_row(
        "W2615P429613",
        source_title=CABINET_SOURCE_TITLE,
        source_desc=CABINET_SOURCE_DESC,
        **kwargs,
    )


def chair_row(**kwargs) -> dict:
    return draft_row(
        "W1097P462002",
        source_title=CHAIR_SOURCE_TITLE,
        source_desc="Soft velvet upholstered lumbar support chair",
        specs={"Frame Material": "EPP Foam"},
        **kwargs,
    )


def run_edit(tmp_path, row, argv_extra, *, store="main"):
    ctx = make_ctx(tmp_path, store=store, db_rows=[row])
    argv = ["--store", store, "--sku", row["sku"], "--report", str(tmp_path / "report.json"), *argv_extra]
    code, out, report = run(argv, ctx)
    return code, out, report, ctx


def load_row(ctx, sku: str) -> dict:
    conn = sqlite3.connect(ctx.db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM collected_products WHERE sku=?", (sku,)).fetchone())
    conn.close()
    if isinstance(row.get("optimization"), str):
        row["optimization"] = json.loads(row["optimization"])
    if isinstance(row.get("images"), str):
        row["images"] = json.loads(row["images"])
    if isinstance(row.get("attributes"), str):
        row["attributes"] = json.loads(row["attributes"]) if row["attributes"] else {}
    if isinstance(row.get("specs"), str):
        row["specs"] = json.loads(row["specs"]) if row["specs"] else {}
    return row


def test_cabinet_title_accept_and_floor_standing_refuse(tmp_path):
    code, out, report, ctx = run_edit(tmp_path, cabinet_row(), ["--set-title", CABINET_ACCEPT_TITLE])
    assert code == 0
    sku = report["skus"][0]
    assert sku["status"] == "planned"
    assert sku["diff"]["title"]["after"] == CABINET_ACCEPT_TITLE
    tokens = {item["token"].casefold(): item for item in sku["diff"]["title"]["token_evidence"]}
    assert tokens['12"']["sourced"] is True
    assert tokens["top"]["sourced"] is True
    assert "token" in out and "ok" in out
    before = load_row(ctx, "W2615P429613")
    assert before["optimization"]["title"] != CABINET_ACCEPT_TITLE

    code, out, report, ctx = run_edit(
        tmp_path / "refuse",
        cabinet_row(),
        ["--set-title", CABINET_REFUSE_TITLE],
    )
    assert code == 2
    blob = out + json.dumps(report)
    assert "title_unsourced_tokens" in blob
    assert "Floor" in blob or "floor" in blob.casefold()
    assert "standing" in blob.casefold()
    assert load_row(ctx, "W2615P429613")["optimization"]["title"] != CABINET_REFUSE_TITLE


def test_cabinet_mounting_aspect_accept_and_freestanding_refuse(tmp_path):
    mounted = cabinet_row(attributes={"Mounting": "Wall Mounted"})
    code, out, report, ctx = run_edit(
        tmp_path,
        mounted,
        ["--set-aspect", "Mounting=Wall Mounted"],
    )
    assert code == 0
    assert report["skus"][0]["diff"]["aspects"]["after"]["Mounting"] == ["Wall Mounted"]

    code, out, report, ctx = run_edit(
        tmp_path / "free",
        mounted,
        ["--set-aspect", "Mounting=Freestanding"],
    )
    assert code == 2
    assert "aspect_unsourced" in (out + json.dumps(report))
    assert "Mounting" not in load_row(ctx, "W2615P429613")["optimization"]["aspects"]

    code, out, report, ctx = run_edit(
        tmp_path / "unnamed",
        cabinet_row(),
        ["--set-aspect", "Mounting=Wall Mounted"],
    )
    assert code == 2
    assert "aspect_no_named_source" in (out + json.dumps(report))


def test_chair_title_and_frame_material(tmp_path):
    code, out, report, ctx = run_edit(tmp_path, chair_row(), ["--set-title", CHAIR_ACCEPT_TITLE])
    assert code == 0
    assert report["skus"][0]["diff"]["title"]["after"] == CHAIR_ACCEPT_TITLE

    code, out, report, ctx = run_edit(
        tmp_path / "barrel",
        chair_row(),
        ["--set-title", CHAIR_REFUSE_TITLE],
    )
    assert code == 2
    blob = (out + json.dumps(report)).casefold()
    assert "title_unsourced_tokens" in blob
    assert "barrel" in blob
    assert "armchair" in blob

    code, out, report, ctx = run_edit(
        tmp_path / "epp",
        chair_row(),
        ["--set-aspect", "Frame Material=EPP Foam"],
    )
    assert code == 0
    assert report["skus"][0]["diff"]["aspects"]["after"]["Frame Material"] == ["EPP Foam"]

    code, out, report, ctx = run_edit(
        tmp_path / "wood",
        chair_row(),
        ["--set-aspect", "Frame Material=Wood"],
    )
    assert code == 2
    assert "aspect_unsourced" in (out + json.dumps(report))


def test_word_boundary_table_not_in_stable():
    row = draft_row("ANY1", source_title="This cabinet is stable with 2 open shelves")
    result = scd.validate_draft_title("Cabinet with 2 Open Shelves Table", row)
    assert result.ok is False
    assert result.reason == "title_unsourced_tokens"
    assert any(token.casefold() == "table" for token in result.unsourced)


def test_unit_aware_47_inch_quote_accept():
    row = draft_row("ANY2", source_title="47 Inch Bathroom Cabinet")
    result = scd.validate_draft_title('47" Bathroom Cabinet', row)
    assert result.ok is True


def test_title_too_long():
    row = draft_row("ANY3", source_title="Cabinet")
    result = scd.validate_draft_title("C" * 81, row)
    assert result.ok is False
    assert result.reason == "title_too_long"


def test_published_row_refused(tmp_path):
    row = cabinet_row(status="PUBLISHED", listing_id="366999000111")
    code, out, report, ctx = run_edit(tmp_path, row, ["--set-title", CABINET_ACCEPT_TITLE])
    assert code == 2
    assert "published_not_editable" in (out + json.dumps(report))
    assert load_row(ctx, row["sku"])["optimization"]["title"] != CABINET_ACCEPT_TITLE


def test_apply_writes_backup_readback_and_preserves_images(tmp_path):
    row = cabinet_row(attributes={"Mounting": "Wall Mounted"})
    code, out, report, ctx = run_edit(
        tmp_path,
        row,
        [
            "--set-title",
            CABINET_ACCEPT_TITLE,
            "--set-aspect",
            "Mounting=Wall Mounted",
            "--apply",
            "--note",
            "source-checked cabinet title",
        ],
    )
    assert code == 0
    written = load_row(ctx, row["sku"])
    assert written["status"] == "READY"
    assert written["listing_id"] in {None, ""}
    assert written["optimization"]["title"] == CABINET_ACCEPT_TITLE
    assert written["optimization"]["aspects"]["Mounting"] == ["Wall Mounted"]
    assert written["images"] == [GIGA_IMAGE]
    assert "x-cc=1" in written["images"][0]
    assert written["title"] == CABINET_SOURCE_TITLE
    assert written["description"] == CABINET_SOURCE_DESC
    assert report["mode"] == "apply"
    assert report["skus"][0]["db_readback"]["ok"] is True
    assert report["backup_dir"]
    assert (tmp_path / "backups").exists()
    conn = sqlite3.connect(ctx.db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "fact_sheet_cache" not in tables


def test_dry_run_does_not_write(tmp_path):
    row = cabinet_row()
    code, out, report, ctx = run_edit(tmp_path, row, ["--set-title", CABINET_ACCEPT_TITLE])
    assert code == 0
    assert report["mode"] == "dry_run"
    assert not report.get("backup_dir")
    assert load_row(ctx, row["sku"])["optimization"]["title"] != CABINET_ACCEPT_TITLE


def test_html_empty_banned_and_connector_only_titles():
    row = cabinet_row()
    assert scd.validate_draft_title("<b>Cabinet</b>", row).reason == "title_has_html"
    assert scd.validate_draft_title("   ", row).reason == "title_empty"
    assert scd.validate_draft_title("with and the", row).reason == "title_no_content_tokens"
    banned = scd.validate_draft_title("Wall Mounted Cabinet Genuine", row, banned_terms=["Genuine"])
    assert banned.reason == "title_banned_term"


def test_ended_and_listing_id_and_draft_statuses(tmp_path):
    code, out, report, ctx = run_edit(
        tmp_path / "ended",
        cabinet_row(status="ENDED", listing_id="1"),
        ["--set-title", CABINET_ACCEPT_TITLE],
    )
    assert code == 2
    assert "published_not_editable" in (out + json.dumps(report))

    code, out, report, ctx = run_edit(
        tmp_path / "listed",
        cabinet_row(status="READY", listing_id="366111222333"),
        ["--set-title", CABINET_ACCEPT_TITLE],
    )
    assert code == 2
    assert "listing_id_present" in (out + json.dumps(report))

    for status in ("COLLECTED", "DRAFT", "PENDING"):
        code, out, report, ctx = run_edit(
            tmp_path / status.lower(),
            cabinet_row(status=status),
            ["--set-title", CABINET_ACCEPT_TITLE],
        )
        assert code == 0, status
        assert report["skus"][0]["status"] == "planned"


def test_push_live_and_multi_sku_and_mixed_ops_refused(tmp_path):
    row = cabinet_row()
    ctx = make_ctx(tmp_path, db_rows=[row])
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            row["sku"],
            "--set-title",
            CABINET_ACCEPT_TITLE,
            "--push-live",
            "--report",
            str(tmp_path / "live.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "source_write_live_forbidden" in (out + json.dumps(report))
    assert ctx.ebay_client is None or getattr(ctx.ebay_client, "puts", []) == []

    ctx = make_ctx(tmp_path / "multi", db_rows=[row, chair_row()])
    code, out, report = run(
        [
            "--store",
            "main",
            "--skus",
            "W2615P429613,W1097P462002",
            "--set-title",
            CABINET_ACCEPT_TITLE,
            "--report",
            str(tmp_path / "multi.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "source_write_single_sku_only" in (out + json.dumps(report))

    code, out, report, ctx = run_edit(
        tmp_path / "mix",
        row,
        ["--set-title", CABINET_ACCEPT_TITLE, "--strip-claim", "floor", "--location", "title"],
    )
    assert code == 2
    assert "source_write_mixed_ops" in (out + json.dumps(report))


def test_existing_set_from_file_still_deletion_only(tmp_path):
    row = cabinet_row(opt_title="Wall Mounted Narrow Bathroom Cabinet 12 inch")
    path = tmp_path / "title.txt"
    path.write_text(CABINET_ACCEPT_TITLE, encoding="utf-8")
    code, out, report, ctx = run_edit(tmp_path, row, ["--set-from-file", f"title={path}"])
    assert code == 2
    assert "text_add_refused" in (out + json.dumps(report)) or "title_would_degrade" in (
        out + json.dumps(report)
    )


def test_unit_equivalents_and_cm_conversion():
    inch_row = draft_row("U1", source_title='TIA 12" Bathroom Cabinet')
    assert scd.validate_draft_title("Bathroom Cabinet 12-inch", inch_row).ok is True
    assert scd.validate_draft_title("Bathroom Cabinet 12 in", inch_row).ok is True

    cm_row = draft_row("U2", source_title="30.48 cm Bathroom Cabinet")
    assert scd.validate_draft_title('12" Bathroom Cabinet', cm_row).ok is True
    assert scd.validate_aspect_value("Length", "12 in", cm_row).ok is True
    assert scd.validate_aspect_value("Length", "13 in", cm_row).ok is False


def test_armchair_does_not_match_chair_and_plural_stem():
    row = draft_row("S1", source_title="Ergonomic chair with 2 shelves")
    assert any(token.casefold() == "armchair" for token in scd.validate_draft_title("Ergonomic Armchair", row).unsourced)
    assert scd.validate_draft_title("Ergonomic Chair with 2 Shelf", row).ok is True


def test_fact_sheet_violation_blocks(tmp_path, monkeypatch):
    def fake(*_args, **kwargs):
        return {
            "status": "violations",
            "violations": [{"claim_type": "semantic_feature", "claim_text": "floor standing", "severity": "HIGH"}],
        }

    monkeypatch.setattr(scd, "check_fact_sheet_violations", fake)
    code, out, report, ctx = run_edit(tmp_path, cabinet_row(), ["--set-title", CABINET_ACCEPT_TITLE])
    assert code == 2
    assert "fact_sheet_violation" in (out + json.dumps(report))
    assert load_row(ctx, "W2615P429613")["optimization"]["title"] != CABINET_ACCEPT_TITLE


def test_claim_guard_is_invoked(tmp_path, monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append(kwargs)
        return {"status": "pass", "violations": []}

    monkeypatch.setattr(scd, "check_fact_sheet_violations", fake)
    code, out, report, ctx = run_edit(tmp_path, cabinet_row(), ["--set-title", CABINET_ACCEPT_TITLE])
    assert code == 0
    assert calls
    assert calls[0]["candidate_title"] == CABINET_ACCEPT_TITLE


def test_maintenance_lock_and_already_absent(tmp_path):
    ctx = make_ctx(tmp_path, db_rows=[cabinet_row()])
    ctx.maintenance_path.write_text("lock", encoding="utf-8")
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W2615P429613",
            "--set-title",
            CABINET_ACCEPT_TITLE,
            "--report",
            str(tmp_path / "m.json"),
        ],
        ctx,
    )
    assert code == 2

    same = cabinet_row(opt_title=CABINET_ACCEPT_TITLE)
    code, out, report, ctx = run_edit(tmp_path / "same", same, ["--set-title", CABINET_ACCEPT_TITLE])
    assert code == 0
    assert report["skus"][0]["status"] == "already_absent"


def test_aspect_replaces_existing_and_hyphen_matches(tmp_path):
    row = cabinet_row(
        attributes={"Mounting": "Wall Mounted"},
        aspects={"Brand": ["AquaVerve"], "Mounting": ["Freestanding"]},
    )
    code, out, report, ctx = run_edit(
        tmp_path,
        row,
        ["--set-aspect", "mounting=Wall-Mounted", "--apply", "--note", "replace mounting"],
    )
    assert code == 0
    written = load_row(ctx, row["sku"])
    assert written["optimization"]["aspects"]["Mounting"] == ["Wall-Mounted"]
    assert written["optimization"]["aspects"]["Brand"] == ["AquaVerve"]


def test_help_mentions_source_checked_mode():
    from scripts.sku_field_edit import build_parser

    text = build_parser().format_help()
    assert "--set-title" in text
    assert "--set-aspect" in text
    assert "--allow-factsheet-unavailable" in text
    assert "local" in text.casefold()
    assert "GIGA" in text or "source" in text.casefold()


def test_refusal_never_exits_zero_and_writes_report(tmp_path):
    code, out, report, ctx = run_edit(
        tmp_path,
        cabinet_row(),
        ["--set-title", "not-a-real-title-zzz"],
    )
    assert code == 2
    assert code != 0
    assert report["skus"][0]["status"] == "refused"
    assert (tmp_path / "report.json").exists()


def test_no_ebay_network_on_source_write(tmp_path, monkeypatch):
    import requests

    def boom(*_args, **_kwargs):
        raise AssertionError("eBay/network must not be called")

    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests.Session, "get", boom)
    monkeypatch.setattr(requests.Session, "put", boom)
    monkeypatch.setattr(requests.Session, "post", boom)
    code, out, report, ctx = run_edit(
        tmp_path,
        cabinet_row(),
        ["--set-title", CABINET_ACCEPT_TITLE, "--apply", "--note", "no network"],
    )
    assert code == 0


def test_crlf_title_is_normalized(tmp_path):
    title = CABINET_ACCEPT_TITLE + "\r"
    code, out, report, ctx = run_edit(tmp_path, cabinet_row(), ["--set-title", title])
    assert code == 0
    assert report["skus"][0]["diff"]["title"]["after"] == CABINET_ACCEPT_TITLE


def test_compound_top_from_countertop_and_ampersand():
    row = draft_row(
        "C1",
        source_title="Bathroom cabinet",
        source_desc="sintered stone countertop and oak & pine",
    )
    assert scd.validate_draft_title("Bathroom Cabinet Stone Top", row).ok is True
    assert scd.validate_draft_title("Bathroom Cabinet oak & pine", row).ok is True


@pytest.mark.parametrize(
    "source_title,draft_title,token",
    [
        ("Adjustable height desk", "Adjustable Table", "table"),
        ("Comfortable lounge", "Comfortable Table", "table"),
        ("Vegetable storage bin", "Vegetable Table", "table"),
        ("Fully assembled lamp", "Assembled LED", "led"),
        ("Covered patio", "Covered Red", "red"),
        ("Featured collection", "Featured Red", "red"),
        ("Lumbar support chair", "Support Port", "port"),
        ("Hardwood cabinet", "Wood Cabinet", "wood"),
        ("Wheelchair ramp", "Chair Ramp", "chair"),
        ("Fiberglass panel", "Glass Panel", "glass"),
        ("Easy to understand", "Understand Stand", "stand"),
        ("Consistent quality", "Consistent Tent", "tent"),
        ("Desktop organizer", "Desktop Top", "top"),
        ("Moonlight lamp", "Moonlight Light", "light"),
    ],
)
def test_compound_suffix_false_friends_refused(source_title, draft_title, token):
    row = draft_row("CF1", source_title=source_title)
    result = scd.validate_draft_title(draft_title, row)
    assert result.ok is False
    assert result.reason == "title_unsourced_tokens"
    assert any(item.casefold() == token for item in result.unsourced)


def test_wood_standalone_is_sourced_but_hardwood_is_not():
    wood = draft_row("W1", source_title="Solid wood cabinet")
    assert scd.validate_draft_title("Wood Cabinet", wood).ok is True
    hard = draft_row("W2", source_title="Hardwood cabinet")
    refused = scd.validate_draft_title("Wood Cabinet", hard)
    assert refused.ok is False
    assert any(item.casefold() == "wood" for item in refused.unsourced)


def test_bookshelf_allowlist_still_sources_shelf():
    row = draft_row("B1", source_title="Oak bookshelf")
    assert scd.validate_draft_title("Oak Shelf", row).ok is True


def test_in_is_not_inches_for_n_in_1_or_color_words():
    combo = draft_row("M1", source_title="2 in 1 folding table")
    quote_from_combo = scd.validate_draft_title('2" folding table', combo)
    assert quote_from_combo.ok is False
    assert any('2"' in item or item.endswith('"') for item in quote_from_combo.unsourced)

    hyphen = draft_row("M2", source_title="2-in-1 folding table")
    assert scd.validate_draft_title('2" folding table', hyphen).ok is False

    quoted = draft_row("M3", source_title='2" folding table')
    combo_from_quote = scd.validate_draft_title("2 in 1 folding table", quoted)
    assert combo_from_quote.ok is False
    assert any(item == "1" for item in combo_from_quote.unsourced)

    color = draft_row("M4", source_title="12 in White cabinet")
    assert scd.validate_draft_title('12" White cabinet', color).ok is False

    real = draft_row("M5", source_title='TIA 12" Bathroom Cabinet')
    assert scd.validate_draft_title("Bathroom Cabinet 12 in", real).ok is True
    assert scd.validate_draft_title("Bathroom Cabinet 12 in x 8 in", real).ok is False
    sized = draft_row("M6", source_title="12 in W x 8 in D cabinet")
    assert scd.validate_draft_title('12" cabinet', sized).ok is True


def test_named_aspect_value_must_equal_whole_field_or_list_item():
    pu = draft_row("A1", source_title="Sofa", attributes={"Material": "PU Leather"})
    assert scd.validate_aspect_value("Material", "Leather", pu).ok is False
    assert scd.validate_aspect_value("Material", "Leather", pu).reason == "aspect_unsourced"
    assert scd.validate_aspect_value("Material", "PU Leather", pu).ok is True

    faux = draft_row("A2", source_title="Sofa", attributes={"Material": "Faux Leather"})
    assert scd.validate_aspect_value("Material", "Leather", faux).reason == "aspect_unsourced"

    upholstery = draft_row(
        "A3",
        source_title="Sofa",
        source_desc="faux leather",
    )
    leather_desc = scd.validate_aspect_value("Upholstery Material", "Leather", upholstery)
    assert leather_desc.ok is False
    assert leather_desc.reason == "aspect_no_named_source"

    off_white = draft_row("A4", source_title="Cabinet", attributes={"Color": "Off-White"})
    assert scd.validate_aspect_value("Color", "White", off_white).reason == "aspect_unsourced"

    listed = draft_row("A5", source_title="Cabinet", attributes={"Color": "Black/White"})
    assert scd.validate_aspect_value("Color", "Black", listed).ok is True
    assert scd.validate_aspect_value("Color", "White", listed).ok is True
    assert scd.validate_aspect_value("Color", "Off-White", listed).ok is False

    epp = draft_row(
        "A6",
        source_title="Ergonomic chair",
        specs={"Frame Material": "EPP Foam"},
    )
    assert scd.validate_aspect_value("Frame Material", "EPP Foam", epp).ok is True
    assert scd.validate_aspect_value("Main Material", "EPP Foam", epp).ok is True
    assert scd.validate_aspect_value("Frame Material", "Foam", epp).reason == "aspect_unsourced"

    color_free = draft_row("A7", source_title="12 in White cabinet")
    assert scd.validate_aspect_value("Color", "White", color_free).reason == "aspect_no_named_source"
    color_named = draft_row("A8", source_title="cabinet", attributes={"Colour": "White"})
    assert scd.validate_aspect_value("Color", "White", color_named).ok is True

    style = draft_row(
        "A9",
        source_title="Storage cabinet",
        source_desc="wood grain finish",
    )
    style_wood = scd.validate_aspect_value("Style", "Wood", style)
    assert style_wood.ok is False
    assert style_wood.reason == "aspect_no_named_source"


def test_named_aspect_rejects_free_text_material_and_accepts_spec():
    pvc = draft_row(
        "A1",
        source_title="Storage cabinet",
        source_desc="wood grain finish",
        attributes={"Material": "PVC"},
    )
    wood = scd.validate_aspect_value("Material", "Wood", pvc)
    assert wood.ok is False
    assert wood.reason == "aspect_unsourced"

    epp = draft_row(
        "A2",
        source_title="Ergonomic chair",
        specs={"Frame Material": "EPP Foam"},
    )
    assert scd.validate_aspect_value("Frame Material", "EPP Foam", epp).ok is True
    assert scd.validate_aspect_value("Main Material", "EPP Foam", epp).ok is True


def test_set_aspect_material_wood_grain_refused_and_frame_material_accepted(tmp_path):
    pvc_row = draft_row(
        "CLI-A1",
        source_title="Storage cabinet",
        source_desc="wood grain finish",
        attributes={"Material": "PVC"},
    )
    code, out, report, ctx = run_edit(tmp_path / "wood", pvc_row, ["--set-aspect", "Material=Wood"])
    assert code == 2
    assert "aspect_unsourced" in (out + json.dumps(report))

    epp_row = draft_row(
        "CLI-A2",
        source_title="Ergonomic chair",
        specs={"Frame Material": "EPP Foam"},
    )
    code, out, report, ctx = run_edit(tmp_path / "epp", epp_row, ["--set-aspect", "Frame Material=EPP Foam"])
    assert code == 0
    assert report["skus"][0]["diff"]["aspects"]["after"]["Frame Material"] == ["EPP Foam"]


@pytest.mark.parametrize(
    "name",
    ["Brand", "MPN", "UPC", "EAN", "ISBN", "Manufacturer Part Number"],
)
def test_protected_aspects_refused(name):
    row = draft_row(
        "P1",
        source_title="AquaVerve cabinet",
        attributes={"Brand": "AquaVerve", "MPN": "X1", "UPC": "1", "EAN": "2", "ISBN": "3"},
    )
    result = scd.validate_aspect_value(name, "AquaVerve", row)
    assert result.ok is False
    assert result.reason == "aspect_protected"


def test_negative_attribute_values_are_not_title_evidence():
    water = draft_row(
        "N1",
        source_title="Outdoor cabinet",
        attributes={"Waterproof": "No"},
    )
    result = scd.validate_draft_title("Waterproof Cabinet", water)
    assert result.ok is False
    assert any(item.casefold() == "waterproof" for item in result.unsourced)

    tools = draft_row(
        "N2",
        source_title="Storage cabinet",
        specs={"Tool-free Assembly": "No"},
    )
    tool_free = scd.validate_draft_title("Tool Free Cabinet", tools)
    assert tool_free.ok is False
    assert any(item.casefold() == "tool" for item in tool_free.unsourced)
    assert any(item.casefold() == "free" for item in tool_free.unsourced)


def test_exact_negative_named_spec_can_be_copied():
    assembly = draft_row(
        "N3",
        source_title="Cabinet",
        specs={"Assembly Required": "No"},
    )
    assert scd.validate_aspect_value("Assembly Required", "No", assembly).ok is True
    assert scd.validate_aspect_value("Assembly Required", "Yes", assembly).reason == "aspect_unsourced"

    water = draft_row(
        "N4",
        source_title="Outdoor cabinet",
        attributes={"Waterproof": "No"},
    )
    assert scd.validate_aspect_value("Waterproof", "No", water).ok is True
    title = scd.validate_draft_title("Waterproof Cabinet", water)
    assert title.ok is False
    assert any(item.casefold() == "waterproof" for item in title.unsourced)


def test_set_of_n_requires_matching_count_phrase():
    drawers = draft_row("C2", source_title="Cabinet with 2 drawers")
    result = scd.validate_draft_title("Set of 2 Chairs", drawers)
    assert result.ok is False
    assert result.reason == "title_count_phrase_unsourced"

    pieces = draft_row("C3", source_title="2-piece dining chairs")
    assert scd.validate_draft_title("Set of 2 Chairs", pieces).ok is True
    assert scd.validate_draft_title("2 Pack Chairs", pieces).ok is True

    bare = draft_row("C4", source_title="Cabinet with shelves")
    set_only = scd.validate_draft_title("Set Cabinet", bare)
    assert set_only.ok is False
    assert any(item.casefold() == "set" for item in set_only.unsourced)

    offset = draft_row("C5", source_title="Cabinet offset of 2 drawers")
    assert scd.extract_count_phrase_numbers(offset["title"]) == set()
    assert scd.validate_draft_title("Set of 2 Chairs", offset).reason == "title_count_phrase_unsourced"


def test_bare_number_plural_noun_is_count_claim():
    legs = draft_row("D1", source_title="Chair frame with 4 legs")
    result = scd.validate_draft_title("4 Dining Chairs", legs)
    assert result.ok is False
    assert result.reason == "title_count_phrase_unsourced"
    assert any("4" in item and "chair" in item for item in result.unsourced)

    chairs = draft_row("D2", source_title="Walnut 4 dining chairs")
    assert scd.validate_draft_title("4 Dining Chairs", chairs).ok is True

    phrase = draft_row("D3", source_title="Set of 4 dining chairs")
    assert scd.validate_draft_title("4 Dining Chairs", phrase).ok is True


def test_fact_sheet_unavailable_blocks_apply_unless_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        scd,
        "check_fact_sheet_violations",
        lambda *_args, **_kwargs: {"status": "unavailable", "violations": []},
    )
    code, out, report, ctx = run_edit(tmp_path / "dry", cabinet_row(), ["--set-title", CABINET_ACCEPT_TITLE])
    assert code == 0
    blob = out + json.dumps(report)
    assert "fact_sheet_unavailable" in blob
    assert "pass" not in {
        gate.get("reason")
        for sku in report["skus"]
        for gate in sku.get("gates", [])
        if gate.get("name") == "claim_guard"
    }
    assert load_row(ctx, "W2615P429613")["optimization"]["title"] != CABINET_ACCEPT_TITLE

    code, out, report, ctx = run_edit(
        tmp_path / "apply",
        cabinet_row(),
        ["--set-title", CABINET_ACCEPT_TITLE, "--apply", "--note", "need factsheet"],
    )
    assert code == 2
    assert "fact_sheet_unavailable" in (out + json.dumps(report))
    assert load_row(ctx, "W2615P429613")["optimization"]["title"] != CABINET_ACCEPT_TITLE

    code, out, report, ctx = run_edit(
        tmp_path / "allow",
        cabinet_row(),
        [
            "--set-title",
            CABINET_ACCEPT_TITLE,
            "--apply",
            "--allow-factsheet-unavailable",
            "--note",
            "explicit unavailable override",
        ],
    )
    assert code == 0
    assert load_row(ctx, "W2615P429613")["optimization"]["title"] == CABINET_ACCEPT_TITLE
