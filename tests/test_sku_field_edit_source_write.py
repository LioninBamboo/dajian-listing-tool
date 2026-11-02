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
    code, out, report, ctx = run_edit(
        tmp_path,
        cabinet_row(),
        ["--set-aspect", "Mounting=Wall Mounted"],
    )
    assert code == 0
    assert report["skus"][0]["diff"]["aspects"]["after"]["Mounting"] == ["Wall Mounted"]

    code, out, report, ctx = run_edit(
        tmp_path / "free",
        cabinet_row(),
        ["--set-aspect", "Mounting=Freestanding"],
    )
    assert code == 2
    assert "aspect_unsourced" in (out + json.dumps(report))
    assert "Mounting" not in load_row(ctx, "W2615P429613")["optimization"]["aspects"]


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
    row = cabinet_row()
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
    assert scd.validate_aspect_value("12 in", cm_row).ok is True
    assert scd.validate_aspect_value("13 in", cm_row).ok is False


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
    row = cabinet_row(aspects={"Brand": ["AquaVerve"], "Mounting": ["Freestanding"]})
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
