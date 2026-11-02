"""Guarded repair / normalize tools: dry-run default, confirm gates, no live writes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.utils.logs_codec import parse_logs
from tests.test_logs_codec import TRAILING_SHORT, TRAILING_XML, VALID


def _make_products_db(path: Path, rows):
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute(
            """
            CREATE TABLE collected_products (
                sku TEXT PRIMARY KEY,
                title TEXT,
                price REAL,
                status TEXT,
                listing_id TEXT,
                optimization TEXT,
                logs TEXT,
                updated_at TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO collected_products "
            "(sku, title, price, status, listing_id, optimization, logs, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _row(sku, *, status="COLLECTED", listing_id=None, logs=VALID, optimization=None, updated_at="2026-10-01"):
    return (sku, f"Title {sku}", 10.0, status, listing_id, optimization, logs, updated_at)


def _statuses(db_path):
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        return {
            sku: (status, logs, updated_at, listing_id)
            for sku, status, logs, updated_at, listing_id in conn.execute(
                "SELECT sku, status, logs, updated_at, listing_id FROM collected_products"
            )
        }
    finally:
        conn.close()


def test_repair_claim_help_does_not_touch_db(tmp_path):
    from scripts.repair_claim_hallucinations import main

    db = _make_products_db(tmp_path / "ebay_collection.db", [_row("N710P1")])
    before = db.read_bytes()
    with pytest.raises(SystemExit) as exited:
        main(["--help"])
    assert exited.value.code == 0
    assert db.read_bytes() == before
    assert _statuses(db)["N710P1"][0] == "COLLECTED"


def test_repair_empty_help_does_not_touch_db(tmp_path):
    from scripts.repair_empty_descriptions import main

    db = _make_products_db(tmp_path / "ebay_collection.db", [_row("N710P1", status="READY")])
    before = db.read_bytes()
    with pytest.raises(SystemExit) as exited:
        main(["--help"])
    assert exited.value.code == 0
    assert db.read_bytes() == before
    assert _statuses(db)["N710P1"][0] == "READY"


def test_repair_claim_dry_run_and_confirm_gate(tmp_path, capsys):
    from scripts.repair_claim_hallucinations import main

    db = _make_products_db(
        tmp_path / "ebay_collection.db",
        [
            _row("SAFE1", status="ERROR", logs="[]"),
            _row("LIVE1", status="PUBLISHED", listing_id="111", logs="[]"),
        ],
    )
    log_file = tmp_path / "issues.json"
    log_file.write_text(
        json.dumps({"issues": [{"sku": "SAFE1"}, {"sku": "LIVE1"}]}),
        encoding="utf-8",
    )
    before = _statuses(db)
    assert main(["--db", str(db), "--log-file", str(log_file)]) == 0
    assert _statuses(db) == before
    assert "counts: matched=2 refused_live=1 writable=1" in capsys.readouterr().out

    assert main(["--db", str(db), "--log-file", str(log_file), "--apply", "--confirm-skus", "2"]) == 2
    assert _statuses(db) == before

    assert main(["--db", str(db), "--log-file", str(log_file), "--apply", "--confirm-skus", "1"]) == 0
    after = _statuses(db)
    assert after["SAFE1"][0] == "COLLECTED"
    assert after["SAFE1"][2] != before["SAFE1"][2]
    assert "repair_claim_hallucinations reset at" in json.loads(after["SAFE1"][1])[-1]
    assert after["LIVE1"][0] == "PUBLISHED"
    assert after["LIVE1"][1] == before["LIVE1"][1]


def test_repair_empty_skips_null_or_non_string_description(tmp_path, capsys):
    from scripts.repair_empty_descriptions import _missing_description, main

    assert _missing_description(json.dumps({"description": None})) is True
    assert _missing_description(json.dumps({"description": {"html": "<p>x</p>"}})) is True
    assert _missing_description(json.dumps({"description": "KEY FEATURES <li>ok</li>"})) is False

    db = _make_products_db(
        tmp_path / "ebay_collection.db",
        [
            _row("NULL-DESC", status="READY", optimization=json.dumps({"description": None})),
            _row("DICT-DESC", status="READY", optimization=json.dumps({"description": {"html": "<p>x</p>"}})),
            _row(
                "BAD-DESC",
                status="READY",
                listing_id=None,
                optimization=json.dumps({"title": "Oak", "description": "<p>no list</p>"}),
            ),
        ],
    )
    before = db.read_bytes()
    assert main(["--db", str(db)]) == 0
    assert db.read_bytes() == before
    assert "counts: matched=3 refused_live=3 writable=0" in capsys.readouterr().out
    after = _statuses(db)
    assert after["NULL-DESC"][0] == "READY"
    assert after["DICT-DESC"][0] == "READY"


def test_repair_empty_confirm_skus_is_writable_not_matched(tmp_path):
    from scripts.repair_empty_descriptions import main

    db = _make_products_db(
        tmp_path / "ebay_collection.db",
        [
            _row(
                "LIVE-BAD",
                status="READY",
                listing_id="111",
                optimization=json.dumps({"description": "<p>no list</p>"}),
            ),
        ],
    )
    before = _statuses(db)
    assert main(["--db", str(db), "--apply", "--confirm-skus", "1"]) == 2
    assert _statuses(db) == before
    assert main(["--db", str(db), "--apply", "--include-live", "--confirm-skus", "1"]) == 0
    assert _statuses(db)["LIVE-BAD"][0] == "COLLECTED"


def test_repair_scripts_refuse_apply_when_maintenance_lock_held(tmp_path):
    from scripts.repair_claim_hallucinations import main as claim_main
    from scripts.repair_empty_descriptions import main as empty_main

    db = _make_products_db(
        tmp_path / "ebay_collection.db",
        [
            _row("SAFE1", status="ERROR", logs="[]"),
            _row(
                "LIVE-BAD",
                status="READY",
                listing_id=None,
                optimization=json.dumps({"description": "<p>no list</p>"}),
            ),
        ],
    )
    lock = tmp_path / "_maintenance.lock"
    lock.write_text("held\n", encoding="utf-8")
    log_file = tmp_path / "issues.json"
    log_file.write_text(json.dumps({"issues": [{"sku": "SAFE1"}]}), encoding="utf-8")
    before = _statuses(db)

    assert (
        claim_main(
            [
                "--db",
                str(db),
                "--log-file",
                str(log_file),
                "--maintenance-lock",
                str(lock),
                "--apply",
                "--confirm-skus",
                "1",
            ]
        )
        == 2
    )
    assert (
        empty_main(
            [
                "--db",
                str(db),
                "--maintenance-lock",
                str(lock),
                "--apply",
                "--include-live",
                "--confirm-skus",
                "1",
            ]
        )
        == 2
    )
    assert _statuses(db) == before


def test_normalize_dry_run_and_apply_only_logs(tmp_path):
    from scripts.normalize_corrupt_logs import main

    db = _make_products_db(
        tmp_path / "ebay_collection.db",
        [
            _row("OK1", status="DELISTED", logs=VALID, updated_at="2026-09-01"),
            _row("BAD1", status="DELISTED", logs=TRAILING_SHORT, updated_at="2026-09-02"),
            _row("BAD2", status="COLLECTED", logs=TRAILING_XML, updated_at="2026-09-03"),
        ],
    )
    missing_lock = tmp_path / "_no_maintenance.lock"
    lock = tmp_path / "_maintenance.lock"
    lock.write_text("parked for normalize\n", encoding="utf-8")
    backups = tmp_path / "backups"
    reports = tmp_path / "logs"
    common = [
        "--db",
        str(db),
        "--maintenance-lock",
        str(lock),
        "--backup-dir",
        str(backups),
        "--report-dir",
        str(reports),
        "--timestamp",
        "20261102_120000",
    ]
    before = _statuses(db)
    assert main(["--db", str(db), "--maintenance-lock", str(missing_lock)]) == 0
    assert _statuses(db) == before

    assert (
        main(
            [
                "--db",
                str(db),
                "--maintenance-lock",
                str(missing_lock),
                "--apply",
                "--store",
                "main",
                "--confirm-count",
                "2",
            ]
        )
        == 2
    )
    assert _statuses(db) == before

    assert main(common + ["--apply", "--store", "main", "--confirm-count", "99"]) == 2
    assert _statuses(db) == before

    assert main(common + ["--apply", "--store", "main", "--confirm-count", "2"]) == 0
    after = _statuses(db)
    assert after["OK1"] == before["OK1"]
    assert after["BAD1"][0] == "DELISTED"
    assert after["BAD1"][2] == "2026-09-02"
    assert after["BAD2"][0] == "COLLECTED"
    assert after["BAD2"][2] == "2026-09-03"
    parsed = parse_logs(after["BAD1"][1])
    assert parsed.status == "ok"
    assert parsed.entries[0] == "Listing: 366407614496, Offer: 108006576011, Cat: 38204"
    assert parsed.entries[1].startswith("[2026-10-08 11:26:48] LONG_OOS_END_LIVE")
    xml = parse_logs(after["BAD2"][1])
    assert xml.status == "ok"
    assert xml.entries[-1].startswith("[2026-10-10 08:35:14] CRO_END_LIVE_20261010")
    assert "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" in xml.entries[-1]
    assert list(backups.glob("normalize_logs_*/ebay_collection.db"))
    report = json.loads(next(reports.glob("normalize_corrupt_logs_main_*.json")).read_text(encoding="utf-8"))
    assert report["count"] == 2
    assert {row["sku"] for row in report["written"]} == {"BAD1", "BAD2"}
