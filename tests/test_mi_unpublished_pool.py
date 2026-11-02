"""Recycle local ENDED rows into the MI unpublished pool."""
from __future__ import annotations

import json
import sqlite3

from src.utils.mi_unpublished_pool import recycle_ended_for_mi
from tests.test_logs_codec import TRAILING_XML


def _make_db(tmp_path, rows):
    db = tmp_path / "ebay_collection.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            title TEXT,
            status TEXT,
            listing_id TEXT,
            cost_breakdown TEXT,
            images TEXT,
            logs TEXT,
            updated_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO collected_products "
        "(sku, title, status, listing_id, cost_breakdown, images, logs, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return str(db)


def _ended(**overrides):
    row = {
        "sku": "END-1",
        "title": "Modern Patio Chair",
        "status": "ENDED",
        "listing_id": "123456",
        "cost_breakdown": json.dumps({"total_dajian_cost": 40.0}),
        "images": json.dumps(["https://a.jpg", "https://b.jpg"]),
        "logs": "[]",
        "updated_at": "2026-08-01",
    }
    row.update(overrides)
    return (
        row["sku"],
        row["title"],
        row["status"],
        row["listing_id"],
        row["cost_breakdown"],
        row["images"],
        row["logs"],
        row["updated_at"],
    )


def _status(db, sku):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT status, listing_id FROM collected_products WHERE sku=?", (sku,)
    ).fetchone()
    conn.close()
    return row


def test_recycles_ended_with_cost_and_images(tmp_path):
    db = _make_db(tmp_path, [_ended()])
    report = recycle_ended_for_mi(db, limit=10, store_kind="furniture")
    assert report["reactivated"] == ["END-1"]
    assert _status(db, "END-1") == ("PENDING", None)


def test_skips_delisted_published_and_zero_cost(tmp_path):
    db = _make_db(
        tmp_path,
        [
            _ended(sku="OK"),
            _ended(sku="DEL", status="DELISTED"),
            _ended(sku="LIVE", status="PUBLISHED"),
            _ended(sku="CHEAP", cost_breakdown=json.dumps({"total_dajian_cost": 0})),
        ],
    )
    report = recycle_ended_for_mi(db, limit=10, store_kind="furniture")
    assert report["reactivated"] == ["OK"]
    assert _status(db, "DEL")[0] == "DELISTED"
    assert _status(db, "LIVE")[0] == "PUBLISHED"
    assert _status(db, "CHEAP")[0] == "ENDED"


def test_skips_auto_titles_blacklist_and_oos(tmp_path):
    db = _make_db(
        tmp_path,
        [
            _ended(sku="HITCH", title="Class 3 Tow Trailer Hitch 2 Inch Receiver"),
            _ended(sku="BLOCKED", title="Patio Bench"),
            _ended(sku="OOS", title="Garden Chair"),
            _ended(sku="KEEP", title="Outdoor Sofa"),
        ],
    )
    report = recycle_ended_for_mi(
        db,
        limit=10,
        store_kind="furniture",
        blacklist={"BLOCKED"},
        stock_lookup=lambda sku: 0 if sku == "OOS" else 5,
    )
    assert report["reactivated"] == ["KEEP"]
    assert report["skipped"]["auto_family"] == 1
    assert report["skipped"]["blacklist"] == 1
    assert report["skipped"]["zero_stock"] == 1


def test_recycle_keeps_trailing_xml_history(tmp_path):
    db = _make_db(tmp_path, [_ended(sku="XML-1", logs=TRAILING_XML)])
    report = recycle_ended_for_mi(db, limit=10, store_kind="furniture")
    assert report["reactivated"] == ["XML-1"]
    conn = sqlite3.connect(db)
    try:
        raw = conn.execute("SELECT logs FROM collected_products WHERE sku='XML-1'").fetchone()[0]
    finally:
        conn.close()
    loaded = json.loads(raw)
    assert loaded[0] == "Category ID: 38208"
    assert loaded[1].startswith("[2026-10-10 08:35:14] CRO_END_LIVE_20261010")
    assert "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" in loaded[1]
    assert loaded[1].split("?>", 1)[1].startswith("\n<EndFixedPriceItemResponse")
    assert loaded[-1].startswith("[MI_ENDED_RECYCLE]")


def test_respects_daily_limit(tmp_path):
    db = _make_db(
        tmp_path,
        [
            _ended(sku="A-CHAIR", title="Patio Chair", updated_at="2026-08-02"),
            _ended(sku="B-SOFA", title="Outdoor Sofa", updated_at="2026-08-03"),
        ],
    )
    report = recycle_ended_for_mi(db, limit=1, store_kind="furniture")
    assert report["reactivated"] == ["B-SOFA"]
    assert _status(db, "A-CHAIR")[0] == "ENDED"
