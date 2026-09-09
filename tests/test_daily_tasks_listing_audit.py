import json
import sqlite3
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
requests_stub = types.SimpleNamespace(
    get=lambda *_args, **_kwargs: None,
    post=lambda *_args, **_kwargs: None,
    utils=types.SimpleNamespace(quote=lambda value: value),
    exceptions=types.SimpleNamespace(HTTPError=Exception),
)
import pytest

@pytest.fixture(autouse=True, scope="module")
def stub_sys_modules():
    stubs = {
        "dotenv": types.SimpleNamespace(load_dotenv=lambda *_args, **_kwargs: None),
        "requests": requests_stub,
        "src.utils.report_images": types.SimpleNamespace(
            build_thumbnail_img_html=lambda *_args, **_kwargs: "",
            normalize_thumbnail_url=lambda url: url,
        ),
        "src.utils.email_sender": types.SimpleNamespace(send_email=lambda *_args, **_kwargs: True),
        "src.services.ebay_category_matcher": types.SimpleNamespace(create_category_matcher=lambda *_args, **_kwargs: object()),
        "src.utils.mi_draft_origin": types.SimpleNamespace(
            MI_DRAFT_ORIGIN="auto",
            apply_mi_draft_origin=lambda *_args, **_kwargs: None,
            append_mi_draft_log=lambda *_args, **_kwargs: None,
        ),
        "src.utils.mi_opportunity_flow": types.SimpleNamespace(
            auto_prepare_mi_opportunity_drafts=lambda *_args, **_kwargs: {},
            empty_auto_prepare_result=lambda: {},
        ),
        "src.clients.real_ebay_client": types.SimpleNamespace(create_real_ebay_client=lambda *_args, **_kwargs: object()),
    }
    
    saved = {}
    pre_existing = set(sys.modules)
    for name, stub in stubs.items():
        if name in sys.modules:
            saved[name] = sys.modules[name]
        sys.modules[name] = stub

    yield

    for name in stubs:
        if name in saved:
            sys.modules[name] = saved[name]
        else:
            sys.modules.pop(name, None)

    # 若 daily_tasks / audit 脚本是在 stub 生效期间被首次 import 的,
    # 它们的 from-import 已把 stub (如恒返回 {} 的
    # auto_prepare_mi_opportunity_drafts) 绑进自身命名空间; 仅还原
    # sys.modules 救不回绑定, 必须移除缓存让后续测试重新导入真实实现
    # (否则 test_mi_e2e_smoke 的 auto-prepare 断言按测试顺序偶发失败).
    for leaked in ("daily_tasks", "scripts.audit_fix_active_listings"):
        if leaked not in pre_existing:
            sys.modules.pop(leaked, None)



def _create_published_db(db_path: Path) -> None:
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
    conn.execute(
        """
        INSERT INTO collected_products (
            sku, title, attributes, specs, optimization, description,
            images, videos, price, suggested_price, listing_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "SKU-FOLD",
            "Wood Cabinet",
            json.dumps({"Color": "Walnut"}),
            json.dumps({"Material": "Wood"}),
            json.dumps(
                {
                    "title": "Stored title",
                    "description": "<div>Stored foldable optimization copy</div>",
                    "aspects": {"Features": ["Wood"]},
                    "categoryId": "111",
                }
            ),
            "<div>Source description without fold support</div>",
            json.dumps(["https://example.com/a.jpg", "https://example.com/b.jpg"]),
            json.dumps(["https://example.com/source.mp4"]),
            99.99,
            109.99,
            "LISTING-1",
            "PUBLISHED",
        ),
    )
    conn.commit()
    conn.close()


def test_run_listing_audit_auto_fix_uses_live_snapshot_and_source_description(monkeypatch, tmp_path):
    import daily_tasks
    from scripts import audit_fix_active_listings

    _create_published_db(tmp_path / "ebay_collection.db")
    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    fake_client = object()
    monkeypatch.setattr(
        sys.modules["src.clients.real_ebay_client"],
        "create_real_ebay_client",
        lambda _env: fake_client,
    )

    captured: dict = {}

    def fake_fetch_live_listing_context(_ebay_client, sku, expected_listing_id=None):
        assert sku == "SKU-FOLD"
        assert expected_listing_id == "LISTING-1"
        return (
            {
                "product": {
                    "title": "Live title",
                    "description": "<div>Live inventory description</div>",
                    "aspects": {"Features": ["Wood"]},
                }
            },
            {
                "offerId": "offer-1",
                "listingDescription": "<div>Live offer description</div>",
                "categoryId": "222",
            },
        )

    def fake_build_live_listing_opt_snapshot(stored_opt, *, inventory_item=None, offer=None):
        assert stored_opt["description"] == "<div>Stored foldable optimization copy</div>"
        assert inventory_item["product"]["title"] == "Live title"
        assert offer["listingDescription"] == "<div>Live offer description</div>"
        return {
            "title": "Live title",
            "description": "<div>Live offer description</div>",
            "aspects": {"Features": ["Wood"]},
            "categoryId": "222",
        }

    def fake_audit_single_product(
        sku,
        title,
        attrs_raw,
        specs_raw,
        opt_raw,
        description,
        ebay_client=None,
        images_raw=None,
        videos_raw=None,
        live_inventory=None,
    ):
        captured["sku"] = sku
        captured["title"] = title
        captured["opt"] = json.loads(opt_raw)
        captured["description"] = description
        captured["images_raw"] = images_raw
        captured["videos_raw"] = videos_raw
        captured["live_inventory"] = live_inventory
        captured["ebay_client"] = ebay_client
        return (
            [{"severity": "CRITICAL", "detail": "live issue"}],
            {"__hallucinated_foldable__": True},
        )

    def fake_fix_listing_on_ebay(
        sku,
        product_row,
        fixes,
        ebay_client,
        db_conn,
        *,
        base_opt_raw=None,
    ):
        captured["fix_sku"] = sku
        captured["fix_product_row"] = product_row
        captured["fixes"] = fixes
        captured["fix_ebay_client"] = ebay_client
        captured["fix_base_opt"] = json.loads(base_opt_raw)
        assert db_conn is not None
        return ["Inventory product fields updated on eBay"]

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_fetch_live_listing_context",
        fake_fetch_live_listing_context,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "build_live_listing_opt_snapshot",
        fake_build_live_listing_opt_snapshot,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "audit_single_product",
        fake_audit_single_product,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "fix_listing_on_ebay",
        fake_fix_listing_on_ebay,
    )

    summary = daily_tasks.run_listing_audit(auto_fix=True)

    assert summary["source"] == "live_ebay"
    assert summary["total_published"] == 1
    assert summary["issues_found"] == 1
    assert summary["fixed"] == 1
    assert summary["errors"] == 0

    assert captured["description"] == "<div>Source description without fold support</div>"
    assert captured["opt"] == {
        "title": "Live title",
        "description": "<div>Live offer description</div>",
        "aspects": {"Features": ["Wood"]},
        "categoryId": "222",
    }
    assert captured["fix_base_opt"] == captured["opt"]
    assert captured["videos_raw"] == json.dumps(["https://example.com/source.mp4"])
    assert captured["live_inventory"]["product"]["title"] == "Live title"
    assert captured["ebay_client"] is fake_client
    assert captured["fix_ebay_client"] is fake_client


def test_run_listing_audit_live_read_only_records_clean_state(monkeypatch, tmp_path):
    import daily_tasks
    from scripts import audit_fix_active_listings

    _create_published_db(tmp_path / "ebay_collection.db")
    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    fake_client = object()
    monkeypatch.setattr(
        sys.modules["src.clients.real_ebay_client"],
        "create_real_ebay_client",
        lambda _env: fake_client,
    )

    def fake_fetch_live_listing_context(_ebay_client, sku, expected_listing_id=None):
        assert sku == "SKU-FOLD"
        assert expected_listing_id == "LISTING-1"
        return (
            {
                "product": {
                    "title": "Live title",
                    "description": "<div>Live inventory description</div>",
                    "aspects": {"Features": ["Wood"]},
                    "imageUrls": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
                    "videoIds": ["video-1"],
                }
            },
            {
                "offerId": "offer-1",
                "listingDescription": "<div>Live offer description</div>",
                "categoryId": "222",
            },
        )

    def fake_build_live_listing_opt_snapshot(_stored_opt, *, inventory_item=None, offer=None):
        assert inventory_item["product"]["title"] == "Live title"
        assert offer["listingDescription"] == "<div>Live offer description</div>"
        return {
            "title": "Live title",
            "description": "<div>Live offer description</div>",
            "aspects": {"Features": ["Wood"]},
            "categoryId": "222",
        }

    def fake_audit_single_product(*_args, **_kwargs):
        return ([], {})

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_fetch_live_listing_context",
        fake_fetch_live_listing_context,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "build_live_listing_opt_snapshot",
        fake_build_live_listing_opt_snapshot,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "audit_single_product",
        fake_audit_single_product,
    )

    summary = daily_tasks.run_listing_audit(
        auto_fix=False,
        use_live=True,
        record_clean_state=True,
    )

    conn = sqlite3.connect(tmp_path / "ebay_collection.db")
    row = conn.execute(
        "SELECT optimization FROM collected_products WHERE sku = ?",
        ("SKU-FOLD",),
    ).fetchone()
    conn.close()

    stored_opt = json.loads(row[0])
    quality_gate = stored_opt["_quality_gate"]

    assert summary["source"] == "live_ebay"
    assert summary["issues_found"] == 0
    assert summary["fixed"] == 0
    assert summary["errors"] == 0
    assert summary["clean_state_recorded"] == 1
    assert summary["skipped_clean_frozen"] == 0
    assert quality_gate["status"] == "clean"
    assert quality_gate["listing_id"] == "LISTING-1"
    assert quality_gate["source_fingerprint"]
    assert quality_gate["live_fingerprint"]


def test_run_listing_audit_skips_live_listing_when_clean_fingerprint_is_frozen(monkeypatch, tmp_path):
    import daily_tasks
    from scripts import audit_fix_active_listings

    db_path = tmp_path / "ebay_collection.db"
    _create_published_db(db_path)
    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    fake_client = object()
    monkeypatch.setattr(
        sys.modules["src.clients.real_ebay_client"],
        "create_real_ebay_client",
        lambda _env: fake_client,
    )

    live_inventory = {
        "product": {
            "title": "Live title",
            "description": "<div>Live inventory description</div>",
            "aspects": {"Features": ["Wood"]},
            "imageUrls": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
            "videoIds": ["video-1"],
        }
    }
    live_offer = {
        "offerId": "offer-1",
        "listingDescription": "<div>Live offer description</div>",
        "categoryId": "222",
    }
    live_opt = {
        "title": "Live title",
        "description": "<div>Live offer description</div>",
        "aspects": {"Features": ["Wood"]},
        "categoryId": "222",
    }

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT title, attributes, specs, description, images, videos, optimization FROM collected_products WHERE sku = ?",
        ("SKU-FOLD",),
    ).fetchone()
    stored_opt = json.loads(row[6])
    source_fingerprint = audit_fix_active_listings.build_audit_fingerprint(
        audit_fix_active_listings.build_source_audit_payload(
            row[0],
            row[3],
            row[1],
            row[2],
            images_raw=row[4],
            videos_raw=row[5],
        )
    )
    live_fingerprint = audit_fix_active_listings.build_audit_fingerprint(
        audit_fix_active_listings.build_live_audit_payload(
            json.dumps(live_opt),
            listing_id="LISTING-1",
            live_inventory=live_inventory,
        )
    )
    stored_opt["_quality_gate"] = {
        "version": audit_fix_active_listings.QUALITY_GATE_META_VERSION,
        "ruleset_version": audit_fix_active_listings.QUALITY_GATE_RULESET_VERSION,
        "status": "clean",
        "listing_id": "LISTING-1",
        "source_fingerprint": source_fingerprint,
        "live_fingerprint": live_fingerprint,
        "verified_clean_at": "2026-06-22T12:00:00",
        "updated_at": "2026-06-22T12:00:00",
    }
    conn.execute(
        "UPDATE collected_products SET optimization = ? WHERE sku = ?",
        (json.dumps(stored_opt, ensure_ascii=False), "SKU-FOLD"),
    )
    conn.commit()
    conn.close()

    def fail_fetch_live_listing_context(*_args, **_kwargs):
        raise AssertionError("incremental audit should skip live fetch for unchanged clean listing")

    def fail_audit_single_product(*_args, **_kwargs):
        raise AssertionError("incremental audit should skip deep audit for unchanged clean listing")

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_fetch_live_listing_context",
        fail_fetch_live_listing_context,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "audit_single_product",
        fail_audit_single_product,
    )

    summary = daily_tasks.run_listing_audit(
        auto_fix=False,
        use_live=True,
        record_clean_state=True,
    )

    assert summary["source"] == "live_ebay"
    assert summary["issues_found"] == 0
    assert summary["fixed"] == 0
    assert summary["skipped_clean_frozen"] == 0
    assert summary["clean_state_recorded"] == 0
    assert summary["audited_live_listings"] == 0
    assert summary["skipped_incremental_scope"] == 1


def test_run_listing_audit_incremental_scope_keeps_dirty_and_pending_verify(monkeypatch, tmp_path):
    import daily_tasks
    from scripts import audit_fix_active_listings

    db_path = tmp_path / "ebay_collection.db"
    _create_published_db(db_path)
    conn = sqlite3.connect(db_path)
    source = conn.execute(
        "SELECT sku, title, attributes, specs, optimization, description, images, videos, "
        "price, suggested_price, listing_id, status FROM collected_products WHERE sku = ?",
        ("SKU-FOLD",),
    ).fetchone()

    base_opt = json.loads(source[4])
    clean_opt = dict(base_opt)
    dirty_opt = dict(base_opt)
    pending_opt = dict(base_opt)
    clean_opt["_quality_gate"] = {
        "version": audit_fix_active_listings.QUALITY_GATE_META_VERSION,
        "ruleset_version": audit_fix_active_listings.QUALITY_GATE_RULESET_VERSION,
        "status": "clean",
        "listing_id": "LISTING-1",
        "source_fingerprint": audit_fix_active_listings.build_audit_fingerprint(
            audit_fix_active_listings.build_source_audit_payload(
                source[1],
                source[5],
                source[2],
                source[3],
                images_raw=source[6],
                videos_raw=source[7],
            )
        ),
        "live_fingerprint": "live-fingerprint",
        "verified_clean_at": "2026-06-22T12:00:00",
        "updated_at": "2026-06-22T12:00:00",
    }
    dirty_opt["_quality_gate"] = {
        "version": audit_fix_active_listings.QUALITY_GATE_META_VERSION,
        "ruleset_version": audit_fix_active_listings.QUALITY_GATE_RULESET_VERSION,
        "status": "dirty",
        "listing_id": "LISTING-2",
        "source_fingerprint": clean_opt["_quality_gate"]["source_fingerprint"],
        "updated_at": "2026-06-22T12:00:00",
    }
    pending_opt["_quality_gate"] = {
        "version": audit_fix_active_listings.QUALITY_GATE_META_VERSION,
        "ruleset_version": audit_fix_active_listings.QUALITY_GATE_RULESET_VERSION,
        "status": "pending_verify",
        "listing_id": "LISTING-3",
        "source_fingerprint": clean_opt["_quality_gate"]["source_fingerprint"],
        "updated_at": "2026-06-22T12:00:00",
    }
    conn.execute("DELETE FROM collected_products")
    conn.executemany(
        "INSERT INTO collected_products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "SKU-CLEAN",
                source[1],
                source[2],
                source[3],
                json.dumps(clean_opt, ensure_ascii=False),
                source[5],
                source[6],
                source[7],
                source[8],
                source[9],
                "LISTING-1",
                source[11],
            ),
            (
                "SKU-DIRTY",
                source[1],
                source[2],
                source[3],
                json.dumps(dirty_opt, ensure_ascii=False),
                source[5],
                source[6],
                source[7],
                source[8],
                source[9],
                "LISTING-2",
                source[11],
            ),
            (
                "SKU-PENDING",
                source[1],
                source[2],
                source[3],
                json.dumps(pending_opt, ensure_ascii=False),
                source[5],
                source[6],
                source[7],
                source[8],
                source[9],
                "LISTING-3",
                source[11],
            ),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sys.modules["src.clients.real_ebay_client"],
        "create_real_ebay_client",
        lambda _env: object(),
    )

    attempted = []

    def fake_fetch_live_listing_context(_client, sku, expected_listing_id=None):
        attempted.append((sku, expected_listing_id))
        return (
            {"product": {"title": f"Live {sku}", "aspects": {}, "imageUrls": [], "videoIds": []}},
            {"listingDescription": f"<div>{sku}</div>", "categoryId": "222"},
        )

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_fetch_live_listing_context",
        fake_fetch_live_listing_context,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "build_live_listing_opt_snapshot",
        lambda _stored_opt, *, inventory_item=None, offer=None: {
            "title": inventory_item["product"]["title"],
            "description": offer["listingDescription"],
            "aspects": {},
            "categoryId": offer["categoryId"],
        },
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "audit_single_product",
        lambda *_args, **_kwargs: ([], {}),
    )

    summary = daily_tasks.run_listing_audit(
        auto_fix=False,
        use_live=True,
        record_clean_state=True,
    )

    assert attempted == [
        ("SKU-DIRTY", "LISTING-2"),
        ("SKU-PENDING", "LISTING-3"),
    ]
    assert summary["audited_live_listings"] == 2
    assert summary["skipped_incremental_scope"] == 1


def test_run_listing_audit_tracks_live_fetch_failure_as_transport_issue(monkeypatch, tmp_path):
    import daily_tasks
    from scripts import audit_fix_active_listings

    _create_published_db(tmp_path / "ebay_collection.db")
    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    fake_client = object()
    monkeypatch.setattr(
        sys.modules["src.clients.real_ebay_client"],
        "create_real_ebay_client",
        lambda _env: fake_client,
    )

    def fake_fetch_live_listing_context(_ebay_client, sku, expected_listing_id=None):
        assert sku == "SKU-FOLD"
        assert expected_listing_id == "LISTING-1"
        raise RuntimeError("token expired")

    def fake_audit_single_product(
        sku,
        title,
        attrs_raw,
        specs_raw,
        opt_raw,
        description,
        **_kwargs,
    ):
        assert sku == "SKU-FOLD"
        assert description == "<div>Source description without fold support</div>"
        return ([], {})

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_fetch_live_listing_context",
        fake_fetch_live_listing_context,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "audit_single_product",
        fake_audit_single_product,
    )

    summary = daily_tasks.run_listing_audit(
        auto_fix=False,
        use_live=True,
        record_clean_state=True,
    )

    conn = sqlite3.connect(tmp_path / "ebay_collection.db")
    row = conn.execute(
        "SELECT optimization FROM collected_products WHERE sku = ?",
        ("SKU-FOLD",),
    ).fetchone()
    conn.close()
    quality_gate = json.loads(row[0])["_quality_gate"]

    assert summary["source"] == "live_ebay"
    assert summary["issues_found"] == 0
    assert summary["critical"] == 0
    assert summary["errors"] == 1
    assert summary["transport_failures"] == 1
    assert summary["top_issues"] == []
    assert summary["top_transport_failures"][0]["sku"] == "SKU-FOLD"
    assert summary["top_transport_failures"][0]["max_severity"] == "HIGH"
    assert quality_gate["status"] == "dirty"
    assert quality_gate["issue_types"] == ["live_fetch_failed"]


def test_run_listing_audit_stops_after_consecutive_live_fetch_failures(monkeypatch, tmp_path):
    import daily_tasks
    from scripts import audit_fix_active_listings

    db_path = tmp_path / "ebay_collection.db"
    _create_published_db(db_path)
    conn = sqlite3.connect(db_path)
    source = conn.execute(
        "SELECT title, attributes, specs, optimization, description, images, videos, "
        "price, suggested_price, listing_id, status FROM collected_products LIMIT 1"
    ).fetchone()
    for index in range(2, 8):
        conn.execute(
            "INSERT INTO collected_products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"SKU-{index}", *source),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(daily_tasks, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setenv("LISTING_AUDIT_NETWORK_FAILURE_LIMIT", "3")
    monkeypatch.setattr(
        sys.modules["src.clients.real_ebay_client"],
        "create_real_ebay_client",
        lambda _env: object(),
    )

    attempted = []

    def fail_fetch(_client, sku, expected_listing_id=None):
        attempted.append(sku)
        raise RuntimeError("network timeout")

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_fetch_live_listing_context",
        fail_fetch,
    )
    monkeypatch.setattr(
        audit_fix_active_listings,
        "audit_single_product",
        lambda *_args, **_kwargs: ([], {}),
    )

    summary = daily_tasks.run_listing_audit(
        auto_fix=False,
        use_live=True,
        record_clean_state=True,
    )

    assert len(attempted) == 3
    assert summary["aborted_network_failures"] is True
    assert summary["unprocessed"] == 4
    assert summary["errors"] == 3
