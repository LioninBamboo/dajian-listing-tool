import json
import sqlite3

import pytest

from src.services.source_refresh import (
    apply_snapshot,
    build_source_snapshot,
    diff_source_row,
    has_mojibake,
    record_source_drift,
    refresh_skus,
)


W3636_DETAIL = {
    "sku": "W3636P456662",
    "productName": "600D Oxford Bell Tent 14.8ft*9.2ft with Stove Jack for Glamping & Family Camping",
    "description": "",
    "characteristics": [
        "Spacious & Large-Capacity Design: 14.8ft*9.2ft size and 9.18-foot top height.",
        "Premium 600D Oxford Cloth Material: fully crafted with 600D Oxford cloth (roof and walls).",
    ],
    "attributes": {"Main Color": "khaki", "Main Material": "oxford fabric"},
    "mainColor": "khaki",
    "mainMaterial": "oxford fabric",
    "assembledLength": "177.16",
    "assembledWidth": "177.16",
    "assembledHeight": "110.24",
    "assembledWeight": "65.48",
    "assembledWeightUnit": "lbs",
    "assembledLengthUnit": "inches",
    "length": "33.07",
    "width": "16.93",
    "height": "14.96",
    "weight": "69.89",
    "weightUnit": "lb",
    "lengthUnit": "in",
    "productVideoUrl": "",
    "videoUrls": [],
    "skuAvailable": True,
    "mpn": "NDK-BT06",
}


def make_row(**overrides):
    row = {
        "title": "600D Oxford Bell Tent 14.8ft*9.2ft with Stove Jack for Glamping & Family Camping",
        "description": (
            "<div>Spacious & Large-Capacity Design: 14.8ft*9.2ft size and 9.18-foot top height. "
            "Premium 600D Oxford Cloth Material: fully crafted with 600D Oxford cloth (roof and walls).</div>"
        ),
        "attributes": json.dumps(
            {
                "Assembled Length (in.)": "177.16",
                "Assembled Width (in.)": "177.16",
                "Assembled Height (in.)": "110.24",
                "Product Weight (lbs.)": "65.48",
                "Main Color": "khaki",
                "Main Material": "oxford fabric",
            }
        ),
        "specs": json.dumps(
            {
                "Package Length (in.)": "33.07",
                "Package Width (in.)": "16.93",
                "Package Height (in.)": "14.96",
                "Package Weight (lbs.)": "69.89",
            }
        ),
        "videos": "[]",
    }
    row.update(overrides)
    return row


@pytest.fixture()
def db_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            attributes TEXT,
            specs TEXT,
            videos TEXT,
            logs TEXT,
            status TEXT,
            updated_at TEXT
        )
        """
    )
    row = make_row()
    conn.execute(
        "INSERT INTO collected_products (sku, title, description, attributes, specs, videos, logs, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "W3636P456662",
            row["title"],
            row["description"],
            row["attributes"],
            row["specs"],
            row["videos"],
            "[]",
            "PUBLISHED",
        ),
    )
    conn.commit()
    yield conn
    conn.close()


class FakeDajianClient:
    def __init__(self, details=None, error=None):
        self.details = details or []
        self.error = error
        self.calls = []

    def get_product_details(self, skus):
        self.calls.append(list(skus))
        if self.error:
            raise self.error
        return self.details


def test_build_snapshot_maps_dims_material_videos_mpn():
    snapshot = build_source_snapshot(W3636_DETAIL)
    assert snapshot is not None
    assert snapshot.title.startswith("600D Oxford Bell Tent")
    assert snapshot.attributes["Assembled Length (in.)"] == "177.16"
    assert snapshot.attributes["Product Weight (lbs.)"] == "65.48"
    assert snapshot.attributes["Main Material"] == "oxford fabric"
    assert snapshot.specs["Package Weight (lbs.)"] == "69.89"
    assert snapshot.videos == []
    assert snapshot.mpn == "NDK-BT06"
    assert "Product Features" in snapshot.description_html
    assert "600D Oxford cloth" in snapshot.description_html


def test_build_snapshot_rejects_empty_detail():
    assert build_source_snapshot(None) is None
    assert build_source_snapshot({}) is None
    assert build_source_snapshot({"sku": "X", "productName": ""}) is None


def test_diff_no_drift_when_snapshot_matches_row():
    snapshot = build_source_snapshot(W3636_DETAIL)
    assert diff_source_row(make_row(), snapshot) == []


def test_diff_detects_title_change():
    detail = dict(W3636_DETAIL, productName="600D Oxford Bell Tent 16ft NEW VERSION")
    snapshot = build_source_snapshot(detail)
    drifts = diff_source_row(make_row(), snapshot)
    title_drifts = [d for d in drifts if d["field"] == "title"]
    assert title_drifts and title_drifts[0]["kind"] == "change"


def test_diff_title_first_refresh_is_normalize_not_change():
    detail = dict(W3636_DETAIL, productName="600D Oxford Bell Tent 16ft NEW VERSION")
    snapshot = build_source_snapshot(detail)
    drifts = diff_source_row(make_row(), snapshot, first_refresh=True)
    title_drifts = [d for d in drifts if d["field"] == "title"]
    assert title_drifts and title_drifts[0]["kind"] == "normalize"


def test_diff_missing_attribute_is_enrichment():
    row = make_row(attributes=json.dumps({"Assembled Length (in.)": "177.16"}))
    snapshot = build_source_snapshot(W3636_DETAIL)
    drifts = diff_source_row(row, snapshot)
    color = [d for d in drifts if d["field"] == "attributes.Main Color"]
    assert color and color[0]["kind"] == "enrichment"


def test_diff_ignores_video_signature_rotation():
    base = "https://b2bfiles1.gigab2b.cn/image/wkseller/1/video.mp4"
    detail = dict(W3636_DETAIL, videoUrls=[f"{base}?x-ct=1783839600&x-cs=new-signature"])
    snapshot = build_source_snapshot(detail)
    row = make_row(videos=json.dumps([f"{base}?x-ct=1776409200&x-cs=old-signature"]))
    assert [d for d in diff_source_row(row, snapshot) if d["field"] == "videos"] == []


def test_diff_detects_video_added():
    detail = dict(W3636_DETAIL, videoUrls=["https://cdn.example.com/tent.mp4"])
    snapshot = build_source_snapshot(detail)
    drifts = diff_source_row(make_row(), snapshot)
    video_drift = [d for d in drifts if d["field"] == "videos"]
    assert video_drift and video_drift[0]["new"] == ["https://cdn.example.com/tent.mp4"]


def test_diff_detects_dimension_change():
    detail = dict(W3636_DETAIL, assembledLength="190.00")
    snapshot = build_source_snapshot(detail)
    drifts = diff_source_row(make_row(), snapshot)
    assert any(d["field"] == "attributes.Assembled Length (in.)" and d["new"] == "190.00" for d in drifts)


def test_diff_flags_mojibake_description_for_repair():
    snapshot = build_source_snapshot(W3636_DETAIL)
    drifts = diff_source_row(make_row(description="��Ʒ��� corrupted snapshot"), snapshot)
    assert any(d["field"] == "description" for d in drifts)


def test_diff_detects_copy_change_via_characteristics():
    detail = dict(
        W3636_DETAIL,
        characteristics=["Brand new waterproof coating with sealed seams."],
    )
    snapshot = build_source_snapshot(detail)
    drifts = diff_source_row(make_row(), snapshot)
    assert any(d["field"] == "description" for d in drifts)


def test_diff_reports_sku_unavailable():
    detail = dict(W3636_DETAIL, skuAvailable=False)
    snapshot = build_source_snapshot(detail)
    drifts = diff_source_row(make_row(), snapshot)
    assert any(d["field"] == "sku_available" for d in drifts)


def test_has_mojibake():
    assert has_mojibake("Boucl� fabric")
    assert not has_mojibake("Boucle fabric")


def test_apply_snapshot_writes_only_drifted_fields(db_conn):
    detail = dict(W3636_DETAIL, videoUrls=["https://cdn.example.com/tent.mp4"])
    snapshot = build_source_snapshot(detail)
    row = dict(
        zip(
            ("title", "description", "attributes", "specs", "videos"),
            db_conn.execute(
                "SELECT title, description, attributes, specs, videos FROM collected_products WHERE sku=?",
                ("W3636P456662",),
            ).fetchone(),
        )
    )
    drifts = diff_source_row(row, snapshot)
    assert apply_snapshot(db_conn, "W3636P456662", snapshot, drifts) is True

    after = db_conn.execute(
        "SELECT videos, description, logs FROM collected_products WHERE sku=?", ("W3636P456662",)
    ).fetchone()
    assert json.loads(after[0]) == ["https://cdn.example.com/tent.mp4"]
    # description did not drift, so the stored snapshot is untouched
    assert after[1] == row["description"]
    assert "Source refresh" in json.loads(after[2])[-1]


def test_apply_snapshot_noop_without_drift(db_conn):
    snapshot = build_source_snapshot(W3636_DETAIL)
    assert apply_snapshot(db_conn, "W3636P456662", snapshot, []) is False


def test_refresh_skus_applies_and_records_drift(db_conn):
    detail = dict(W3636_DETAIL, videoUrls=["https://cdn.example.com/tent.mp4"])
    client = FakeDajianClient(details=[detail])
    summary = refresh_skus(db_conn, client, ["W3636P456662"], apply=True, context="test")
    assert summary["checked"] == 1
    assert "W3636P456662" in summary["drifted"]
    assert summary["applied"] == ["W3636P456662"]
    drift_rows = db_conn.execute("SELECT sku, context FROM source_drift_log").fetchall()
    assert drift_rows == [("W3636P456662", "test")]


def test_refresh_skus_fetch_failure_never_clobbers(db_conn):
    before = db_conn.execute(
        "SELECT title, description, attributes, videos FROM collected_products WHERE sku=?",
        ("W3636P456662",),
    ).fetchone()
    client = FakeDajianClient(error=ConnectionError("boom"))
    summary = refresh_skus(db_conn, client, ["W3636P456662"], apply=True, context="test")
    assert summary["fetch_failed"] == ["W3636P456662"]
    after = db_conn.execute(
        "SELECT title, description, attributes, videos FROM collected_products WHERE sku=?",
        ("W3636P456662",),
    ).fetchone()
    assert after == before


def test_refresh_skus_dry_run_records_but_does_not_apply(db_conn):
    detail = dict(W3636_DETAIL, videoUrls=["https://cdn.example.com/tent.mp4"])
    client = FakeDajianClient(details=[detail])
    summary = refresh_skus(db_conn, client, ["W3636P456662"], apply=False, context="test")
    assert "W3636P456662" in summary["drifted"]
    assert summary["applied"] == []
    videos = db_conn.execute(
        "SELECT videos FROM collected_products WHERE sku=?", ("W3636P456662",)
    ).fetchone()[0]
    assert json.loads(videos) == []


def test_record_source_drift_skips_empty(db_conn):
    record_source_drift(db_conn, "W3636P456662", [], "test")
    tables = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_drift_log'"
    ).fetchall()
    assert tables == []
