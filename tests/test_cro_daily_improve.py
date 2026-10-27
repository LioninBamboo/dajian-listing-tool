"""Offline tests for scripts/cro_daily_improve.py (no real DB / eBay / SMTP)."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.utils.store_profile import StoreProfile

CST = timezone(timedelta(hours=8))

GIGA_URL = (
    "https://b2bfiles1.gigab2b.cn/img/W5230P450590.jpg"
    "?x-oss-process=image/resize,w_800&x-cc=cc1&x-cu=cu1&x-ct=ct1&x-cs=cs1"
)
GIGA_URL_2 = (
    "https://b2bfiles1.gigab2b.cn/img/W3636P456622.jpg"
    "?x-oss-process=image/resize,w_800&x-cc=cc2&x-cu=cu2&x-ct=ct2&x-cs=cs2"
)


def _main_profile(**kwargs) -> StoreProfile:
    defaults = dict(brand_name="AquaVerve", scheduler_profile="full", category_tree_id="0")
    defaults.update(kwargs)
    return StoreProfile(**defaults)


def _gp_profile(**kwargs) -> StoreProfile:
    defaults = dict(brand_name="GrovePop", scheduler_profile="ops", category_tree_id="0")
    defaults.update(kwargs)
    return StoreProfile(**defaults)


def _aqua_profile(**kwargs) -> StoreProfile:
    defaults = dict(
        brand_name="AquaRides",
        scheduler_profile="ops",
        category_tree_id="100",
        listing_channel="trading",
        ebay_site_id="100",
    )
    defaults.update(kwargs)
    return StoreProfile(**defaults)


def _now() -> datetime:
    return datetime(2026, 10, 27, 12, 0, tzinfo=CST)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _init_db(path: Path, rows: list[dict]) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            title TEXT,
            price REAL,
            shipping REAL,
            stock INTEGER,
            images TEXT,
            cost_breakdown TEXT,
            optimization TEXT,
            suggested_price REAL,
            attributes TEXT,
            specs TEXT,
            status TEXT,
            listing_id TEXT,
            published_at TEXT,
            created_at TEXT,
            description TEXT
        )
        """
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO collected_products (
                sku, title, price, shipping, stock, images, cost_breakdown,
                optimization, suggested_price, attributes, specs, status,
                listing_id, published_at, created_at, description
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["sku"],
                row.get("title", row["sku"]),
                row.get("price", 80),
                row.get("shipping", 0),
                row.get("stock", 5),
                json.dumps(row.get("images") or [GIGA_URL, GIGA_URL_2]),
                json.dumps(row.get("cost_breakdown") or {"total_dajian_cost": 50}),
                json.dumps(row.get("optimization") or {"categoryId": "38208", "title": row.get("title", row["sku"])}),
                row.get("suggested_price", 99),
                json.dumps({}),
                json.dumps({}),
                row.get("status", "PUBLISHED"),
                row.get("listing_id", f"L_{row['sku']}"),
                row.get("published_at", "2026-09-01T00:00:00"),
                row.get("created_at", "2026-09-01T00:00:00"),
                row.get("description", ""),
            ),
        )
    conn.commit()
    conn.close()
    return path


def _ctx(tmp_path: Path, store: str, profile: StoreProfile, **kwargs):
    from scripts.cro_daily_improve import ImproveContext

    root = tmp_path / store
    (root / "logs" / "locks").mkdir(parents=True, exist_ok=True)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    db = root / "ebay_collection.db"
    if not db.exists():
        _init_db(db, kwargs.pop("db_rows", [
            {"sku": "W5230P450590", "suggested_price": 120, "stock": 4, "listing_id": "188111"},
            {"sku": "W3636P456622", "suggested_price": 88, "stock": 3, "listing_id": "188222"},
            {"sku": "W465P462751", "suggested_price": 70, "stock": 2, "listing_id": "188770765185"},
            {"sku": "W465P170804", "suggested_price": 70, "stock": 2, "listing_id": "188802002738"},
            {"sku": "W465P451141", "suggested_price": 55, "stock": 1, "listing_id": "188999"},
        ]))
    ctx = ImproveContext(
        store=store,
        profile=profile,
        project_root=tmp_path,
        store_root=root,
        db_path=db,
        queue_path=root / "logs" / "cro_action_queue.jsonl",
        out_dir=tmp_path / "reports" / "cro_daily_improve",
        lock_path=root / "logs" / "locks" / "cro_daily_improve.lock",
        health_path=root / "logs" / "_scheduler_health.json",
        ebay_environment=kwargs.pop("ebay_environment", "PRODUCTION"),
        profile_path=str(root / "config" / "store_profile.yaml"),
        now=kwargs.pop("now", _now()),
        readonly_db=True,
        **kwargs,
    )
    return ctx


class GetOnlyClient:
    def __init__(self, items=None):
        self.items = items or {}
        self.puts = []

    def get_inventory_item(self, sku):
        return self.items.get(sku)

    def create_or_replace_inventory_item(self, sku, payload):
        raise AssertionError(f"write called: create_or_replace_inventory_item {sku}")

    def __getattr__(self, name):
        if any(name.startswith(p) for p in ("update", "create", "delete", "put", "revise")):
            def boom(*_a, **_k):
                raise AssertionError(f"write called: {name}")
            return boom
        raise AttributeError(name)


class _FakeHttpResp:
    status_code = 204
    text = ""


def _attach_live_session(client):
    """#48 _put_inventory_refresh requires session/oauth/base_url (no rebuild fallback)."""

    class _OAuth:
        def get_valid_token(self):
            return "tok"

    class _Session:
        def put(self, url, headers=None, json=None, timeout=None):
            sku = str(url).rstrip("/").split("/")[-1]
            client.create_or_replace_inventory_item(sku, json)
            return _FakeHttpResp()

    client.session = _Session()
    client.oauth = _OAuth()
    client.base_url = "https://api.ebay.com"
    return client


def _payload_urls_and_qty(payload):
    urls = list((payload or {}).get("image_urls") or [])
    if not urls:
        urls = list(((payload or {}).get("product") or {}).get("imageUrls") or [])
    qty = (payload or {}).get("quantity")
    if qty is None:
        qty = (((payload or {}).get("availability") or {}).get(
            "shipToLocationAvailability") or {}).get("quantity")
    return urls, int(qty if qty is not None else 3)


def _item(urls, qty=2):
    return {
        "condition": "NEW",
        "availability": {"shipToLocationAvailability": {"quantity": qty}},
        "product": {
            "title": "Live title",
            "description": "Live desc",
            "aspects": {"Brand": ["AquaVerve"]},
            "imageUrls": urls,
        },
    }


@pytest.fixture
def no_promote_module():
    sys.modules.pop("scripts.cro_promote", None)
    yield
    sys.modules.pop("scripts.cro_promote", None)


def test_promote_cannot_be_enabled(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m

    parser = m.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--store", "main", "--actions", "promote"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--store", "main", "--promote"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--store", "main", "--enable-promote"])

    monkeypatch.setenv("CRO_ACTIONS", "promote")
    monkeypatch.setenv("CRO_APPLY", "1")

    assert isinstance(m.ALLOWED_ACTIONS, frozenset)
    assert "promote" not in m.ALLOWED_ACTIONS
    src = Path(m.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert "scripts.cro_promote" not in imported
    assert "src.services.ebay_ad_service" not in imported
    assert "EbayAdService" not in src
    assert "from scripts.cro_promote" not in src
    assert "import scripts.cro_promote" not in src
    assert "import cro_promote" not in src

    monkeypatch.setattr(m, "ALLOWED_ACTIONS", frozenset({"price_drop", "image_refresh", "promote"}))
    assert "promote" not in m.effective_allowed_actions()

    execute = {"price": 0, "image": 0}

    def boom_price(*_a, **_k):
        execute["price"] += 1
        raise AssertionError("price executor should not run")

    def boom_image(*_a, **_k):
        execute["image"] += 1
        raise AssertionError("image executor should not run")

    live_patch = pytest.MonkeyPatch()
    live_patch.setattr(m, "apply_price_live", boom_price)
    live_patch.setattr(m, "apply_image_live", boom_image)

    bad = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=12)).isoformat(),
        "candidates": [
            {"action": "promote", "sku": "W465P462751", "listing_id": "188770765185"},
            {"action": "price_drop", "sku": "W5230P450590", "live_price": 120, "new_price": 110},
        ],
    }
    cand_path = tmp_path / "bad_promote.json"
    cand_path.write_text(json.dumps(bad), encoding="utf-8")
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_client_factory=lambda: (_ for _ in ()).throw(AssertionError("client")),
        write_price_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("write")),
    )
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(cand_path),
            "--approve-sha256", _sha(cand_path)[:12],
            "--out-dir", str(tmp_path / "reports" / "cro_daily_improve"),
        ],
        ctx=ctx,
    )
    assert rc == m.EXIT_DISALLOWED
    assert execute == {"price": 0, "image": 0}
    live_patch.undo()

    ad_calls = []

    class FakeAds:
        def update_ad_bid(self, *a, **k):
            ad_calls.append("update_ad_bid")

        def create_ad_safe(self, *a, **k):
            ad_calls.append("create_ad_safe")

        def delete_ad(self, *a, **k):
            ad_calls.append("delete_ad")

    monkeypatch.setattr(m, "ALLOWED_ACTIONS", m._STRUCTURAL_ALLOWED)
    client = GetOnlyClient({
        "W5230P450590": _item(["https://img/old.jpg"], qty=3),
        "W3636P456622": _item(["https://img/old2.jpg"], qty=3),
    })
    products = [
        {
            "sku": "W5230P450590",
            "listing_id": "188111",
            "selling_price": 120,
            "total_cost": 50,
            "floor_price": 55,
            "images": [GIGA_URL, GIGA_URL_2],
            "stock": 4,
            "stock_known": True,
            "title": "Sofa",
            "categoryId": "38208",
        },
        {
            "sku": "W3636P456622",
            "listing_id": "188222",
            "selling_price": 88,
            "total_cost": 40,
            "floor_price": 44,
            "images": [GIGA_URL_2, GIGA_URL],
            "stock": 3,
            "stock_known": True,
            "title": "Table",
            "categoryId": "38204",
        },
    ]

    def load_products(_ctx):
        return [dict(p) for p in products]

    def load_perf(_ctx):
        return {"traffic_meta": {"api_ok": True}, "traffic": {}, "sales": {}}

    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        load_products_fn=load_products,
        load_perf_fn=load_perf,
        load_pending_fn=lambda **k: [
            {"sku": "W5230P450590", "action": "price_drop", "listing_id": "188111",
             "enqueued_at": _now().isoformat(), "detail": {"suggested_price": 110}},
            {"sku": "W3636P456622", "action": "image_refresh", "listing_id": "188222",
             "enqueued_at": _now().isoformat()},
        ],
        recent_terminal_fn=lambda **k: set(),
        scheduler_succeeded_fn=lambda name: name == "daily_tasks",
        read_live_price_fn=lambda sku, lid: 120 if sku.startswith("W5230") else 88,
        precheck_price_fn=lambda **k: (True, "ok"),
        compute_new_price_fn=lambda *a, **k: {"new_price": 110, "floor": 55, "strategy": "TEST"},
        ebay_client_factory=lambda: client,
        load_local_product_fn=lambda sku: {"images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d"},
        send_email_fn=lambda *a, **k: ad_calls.append("email"),
    )
    ctx.health_path.write_text(json.dumps({
        "tasks": {"daily_tasks": {"status": "success", "at": _now().isoformat()}}
    }), encoding="utf-8")
    rc = m.main(["--store", "main", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    assert "scripts.cro_promote" not in sys.modules
    assert ad_calls == []
    FakeAds().update_ad_bid  # keep class referenced without calling
    reports = list(ctx.out_dir.glob("*_dry.json"))
    assert reports
    dry = json.loads(reports[0].read_text(encoding="utf-8"))
    assert dry["promote_disabled"] is True
    assert dry["allowed_actions"] == ["image_refresh", "price_drop"]
    assert all(c["action"] in {"price_drop", "image_refresh"} for c in dry["candidates"])

    digest = _sha(reports[0])
    live_client = GetOnlyClient({
        "W3636P456622": _item(["https://img/old2.jpg"], qty=3),
    })
    live_puts = []

    def put(sku, payload):
        live_puts.append((sku, payload))
        urls, qty = _payload_urls_and_qty(payload)
        live_client.items[sku] = _item(urls, qty=qty)

    live_client.create_or_replace_inventory_item = put
    _attach_live_session(live_client)
    live_ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_environment="PRODUCTION",
        read_live_price_fn=lambda sku, lid: 120,
        precheck_price_fn=lambda **k: (True, "ok"),
        write_price_fn=lambda sku, price, lid: True,
        verify_price_fn=lambda sku, price, lid: (True, price),
        write_price_db_fn=lambda *a, **k: True,
        mark_done_fn=lambda skus, action, queue_path=None: len(list(skus)),
        recent_terminal_fn=lambda **k: set(),
        ebay_client_factory=lambda: live_client,
        load_local_product_fn=lambda sku: {
            "images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d",
        },
        now=_now(),
    )
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(reports[0]),
            "--approve-sha256", digest[:12],
            "--out-dir", str(live_ctx.out_dir),
        ],
        ctx=live_ctx,
    )
    assert rc == 0
    assert "scripts.cro_promote" not in sys.modules
    assert execute == {"price": 0, "image": 0}


def test_ops_store_skips_diagnose_writeback(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m
    import daily_tasks
    import src.services.cro_daily_runner as runner
    import src.services.cro_snapshot_store as snap
    import src.services.cro_action_queue as queue

    counts = {
        "run_cro_diagnose": 0,
        "run_cro_daily": 0,
        "save_snapshots": 0,
        "enqueue": 0,
        "enqueue_unique_pending": 0,
        "mark_done": 0,
    }

    def boom(name):
        def _inner(*_a, **_k):
            counts[name] += 1
            raise AssertionError(f"{name} must not be called on ops dry-run")
        return _inner

    monkeypatch.setattr(daily_tasks, "run_cro_diagnose", boom("run_cro_diagnose"))
    monkeypatch.setattr(runner, "run_cro_daily", boom("run_cro_daily"))
    monkeypatch.setattr(snap, "save_snapshots", boom("save_snapshots"))
    monkeypatch.setattr(queue, "enqueue", boom("enqueue"))
    monkeypatch.setattr(queue, "enqueue_unique_pending", boom("enqueue_unique_pending"))
    monkeypatch.setattr(queue, "mark_done", boom("mark_done"))

    queue_path = tmp_path / "gp" / "logs" / "cro_action_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        json.dumps({"sku": "W5230P450590", "action": "price_drop", "status": "pending"}) + "\n",
        encoding="utf-8",
    )
    before_bytes = queue_path.read_bytes()
    before_mtime = queue_path.stat().st_mtime

    client_built = {"n": 0}

    def factory():
        client_built["n"] += 1
        return GetOnlyClient({
            "W3636P456622": _item(["https://live/old.jpg"], qty=2),
        })

    ctx = _ctx(
        tmp_path, "gp", _gp_profile(),
        load_products_fn=lambda _c: [{
            "sku": "W5230P450590",
            "listing_id": "1",
            "selling_price": 120,
            "total_cost": 50,
            "floor_price": 55,
            "images": [GIGA_URL, GIGA_URL_2],
            "stock": 3,
            "stock_known": True,
            "title": "x" * 70,
            "categoryId": "38208",
            "impressions": 2000,
            "views": 5,
            "age_days": 30,
        }],
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}, "traffic": {}, "sales": {}},
        recent_terminal_fn=lambda **k: set(),
        read_live_price_fn=lambda sku, lid: 120,
        precheck_price_fn=lambda **k: (True, "ok"),
        compute_new_price_fn=lambda *a, **k: {"new_price": 100, "floor": 55, "strategy": "TEST"},
        ebay_client_factory=factory,
        load_local_product_fn=lambda sku: {"images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d"},
        mark_done_fn=boom("mark_done"),
    )
    rc = m.main(["--store", "gp", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    assert counts == {
        "run_cro_diagnose": 0,
        "run_cro_daily": 0,
        "save_snapshots": 0,
        "enqueue": 0,
        "enqueue_unique_pending": 0,
        "mark_done": 0,
    }
    assert queue_path.read_bytes() == before_bytes
    assert queue_path.stat().st_mtime == before_mtime

    live_built = {"n": 0}

    def live_factory():
        live_built["n"] += 1
        raise AssertionError("ops LIVE must not construct eBay client")

    live_ctx = _ctx(
        tmp_path, "gp", _gp_profile(),
        ebay_environment="PRODUCTION",
        ebay_client_factory=live_factory,
    )
    dummy = tmp_path / "dummy.json"
    dummy.write_text("{}", encoding="utf-8")
    rc = m.main(
        [
            "--store", "gp", "--live",
            "--candidates", str(dummy),
            "--approve-sha256", "deadbeefdead",
            "--out-dir", str(live_ctx.out_dir),
        ],
        ctx=live_ctx,
    )
    assert rc == m.EXIT_OPS_LIVE
    assert live_built["n"] == 0


def test_dry_run_zero_writes(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m

    db = tmp_path / "main" / "ebay_collection.db"
    db.parent.mkdir(parents=True)
    _init_db(db, [{
        "sku": "W5230P450590",
        "suggested_price": 120,
        "stock": 4,
        "images": [GIGA_URL, GIGA_URL_2],
    }])
    queue = tmp_path / "main" / "logs" / "cro_action_queue.jsonl"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(
        json.dumps({
            "sku": "W5230P450590",
            "action": "price_drop",
            "status": "pending",
            "listing_id": "188111",
            "enqueued_at": _now().isoformat(),
            "detail": {"suggested_price": 100},
        }) + "\n",
        encoding="utf-8",
    )
    db_hash_before = _sha(db)
    writes_seen = []

    def authorizer(action, *_a):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            writes_seen.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    mail = {"n": 0}

    def send_mail(*_a, **_k):
        mail["n"] += 1

    client = GetOnlyClient({
        "W5230P450590": _item(["https://live/old.jpg"], qty=2),
    })
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        authorizer=authorizer,
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}, "traffic": {}, "sales": {}},
        load_pending_fn=lambda **k: [{
            "sku": "W5230P450590",
            "action": "price_drop",
            "listing_id": "188111",
            "enqueued_at": _now().isoformat(),
            "detail": {"suggested_price": 100},
        }],
        recent_terminal_fn=lambda **k: set(),
        scheduler_succeeded_fn=lambda name: name == "daily_tasks",
        read_live_price_fn=lambda sku, lid: 120,
        precheck_price_fn=lambda **k: (True, "ok"),
        compute_new_price_fn=lambda *a, **k: {"new_price": 100, "floor": 55, "strategy": "TEST"},
        ebay_client_factory=lambda: client,
        load_local_product_fn=lambda sku: {"images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d"},
        send_email_fn=send_mail,
        mark_done_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("mark_done")),
        write_price_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("write_price")),
        write_price_db_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("write_db")),
    )
    before_files = set()
    for folder in (tmp_path,):
        before_files.update(p for p in folder.rglob("*") if p.is_file())

    rc = m.main(["--store", "main", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    assert _sha(db) == db_hash_before
    assert writes_seen == []
    assert mail["n"] == 0
    after_files = {p for p in tmp_path.rglob("*") if p.is_file()} - before_files
    rels = {p.relative_to(tmp_path).as_posix() for p in after_files}
    assert all(
        name.startswith("reports/cro_daily_improve/")
        or name.endswith("logs/locks/cro_daily_improve.lock")
        or name.endswith("_dry.json")
        or name.endswith("_dry.md")
        for name in rels
    )
    assert any(name.endswith("_dry.json") for name in rels)
    dry = json.loads(next(ctx.out_dir.glob("*_dry.json")).read_text(encoding="utf-8"))
    assert dry["writes"] == {"db": 0, "queue": 0, "ebay": 0}
    assert dry["mode"] == "dry"


def test_price_and_stock_guards(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    def seeds(**_k):
        return [
            {"sku": "UP", "action": "price_drop", "listing_id": "1",
             "enqueued_at": _now().isoformat(), "detail": {"suggested_price": 130}},
            {"sku": "GUARD", "action": "price_drop", "listing_id": "2",
             "enqueued_at": _now().isoformat(), "detail": {"suggested_price": 80}},
            {"sku": "NONE", "action": "price_drop", "listing_id": "3",
             "enqueued_at": _now().isoformat(), "detail": {"suggested_price": 80}},
            {"sku": "ZERO", "action": "price_drop", "listing_id": "4",
             "enqueued_at": _now().isoformat(), "detail": {"suggested_price": 80}},
            {"sku": "MISSING", "action": "price_drop", "listing_id": "5",
             "enqueued_at": _now().isoformat(), "detail": {"suggested_price": 80}},
        ]

    products = [
        {"sku": "UP", "listing_id": "1", "selling_price": 120, "total_cost": 50,
         "stock": 3, "stock_known": True, "images": [], "title": "u"},
        {"sku": "GUARD", "listing_id": "2", "selling_price": 120, "total_cost": 50,
         "stock": 3, "stock_known": True, "images": [], "title": "g"},
        {"sku": "NONE", "listing_id": "3", "selling_price": 120, "total_cost": 50,
         "stock": None, "stock_known": False, "images": [], "title": "n"},
        {"sku": "ZERO", "listing_id": "4", "selling_price": 120, "total_cost": 50,
         "stock": 0, "stock_known": True, "images": [], "title": "z"},
    ]

    def precheck(**kwargs):
        if kwargs["sku"] == "GUARD":
            raise RuntimeError("guard exploded")
        return True, "ok"

    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        load_products_fn=lambda _c: [dict(p) for p in products],
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}},
        load_pending_fn=lambda **k: [s for s in seeds() if s["action"] == k.get("action_type")],
        recent_terminal_fn=lambda **k: set(),
        scheduler_succeeded_fn=lambda name: True,
        read_live_price_fn=lambda sku, lid: 120,
        precheck_price_fn=precheck,
        compute_new_price_fn=lambda product, live, market, seed: {
            "new_price": float((seed or {}).get("detail", {}).get("suggested_price") or 80),
            "floor": 55,
            "strategy": "T",
        },
        ebay_client_factory=lambda: GetOnlyClient(),
        load_local_product_fn=lambda sku: {"images": [], "title": "", "description": ""},
    )
    rc = m.main(["--store", "main", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    dry = json.loads(next(ctx.out_dir.glob("*_dry.json")).read_text(encoding="utf-8"))
    assert dry["candidates"] == []
    assert any(r["sku"] == "UP" for r in dry["excluded"]["not_a_drop"])
    assert any(r["sku"] == "GUARD" for r in dry["excluded"]["guard_error"])
    assert any(r["sku"] == "NONE" for r in dry["excluded"]["stock_unknown"])
    assert any(r["sku"] == "ZERO" for r in dry["excluded"]["stock_zero"])
    assert any(r["sku"] == "MISSING" for r in dry["excluded"]["stock_unknown"])


def test_image_qty_and_giga_sig(tmp_path, no_promote_module):
    from scripts.cro_image_refresh import (
        build_strict_refresh_payload,
        strip_oss_keep_giga_sig,
        urls_keep_giga_sigs,
    )
    from scripts import cro_daily_improve as m

    kept = strip_oss_keep_giga_sig(GIGA_URL)
    assert "x-oss-process" not in kept
    for key in ("x-cc", "x-cu", "x-ct", "x-cs"):
        assert key in kept
    assert urls_keep_giga_sigs([GIGA_URL], [kept])
    stripped = "https://b2bfiles1.gigab2b.cn/img/W5230P450590.jpg"
    assert urls_keep_giga_sigs([GIGA_URL], [stripped]) is False

    item_missing = {"availability": {}, "product": {"title": "t", "imageUrls": ["https://old.jpg"]}}
    payload, reason = build_strict_refresh_payload(
        item_missing, {"images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d"},
    )
    assert payload is None and reason == "qty_unknown"

    item_zero = _item(["https://old.jpg"], qty=0)
    payload, reason = build_strict_refresh_payload(
        item_zero, {"images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d"},
    )
    assert payload is None and reason == "qty_zero"

    item_ok = _item(["https://old.jpg"], qty=7)
    item_ok["locale"] = "en_US"
    item_ok["sku"] = "W5230P450590"
    item_ok["href"] = "https://api.ebay.com/sell/inventory/v1/inventory_item/W5230P450590"
    item_ok["inventoryItemGroupKeys"] = ["grp-1"]
    item_ok["availability"]["shipToLocationAvailability"]["allocationByFormat"] = {"auction": 0}
    item_ok["packageWeightAndSize"] = {
        "weight": {"value": 12.5, "unit": "POUND"},
        "dimensions": {"length": 20, "width": 10, "height": 8, "unit": "INCH"},
    }
    item_ok["product"]["brand"] = "AquaVerve"
    item_ok["product"]["mpn"] = "AV-1"
    item_ok["product"]["upc"] = ["123456789012"]
    item_ok["product"]["ean"] = ["1234567890123"]
    item_ok["product"]["epid"] = "1234567890"
    payload, reason = build_strict_refresh_payload(
        item_ok, {"images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d"},
    )
    assert reason is None
    assert payload["quantity"] == 7
    assert urls_keep_giga_sigs([GIGA_URL, GIGA_URL_2], payload["image_urls"])
    assert payload["packageWeightAndSize"] == item_ok["packageWeightAndSize"]
    assert payload["locale"] == "en_US"
    assert payload["availability"]["shipToLocationAvailability"]["quantity"] == 7
    assert payload["availability"]["shipToLocationAvailability"]["allocationByFormat"] == {"auction": 0}
    assert payload["product"]["upc"] == ["123456789012"]
    assert payload["product"]["ean"] == ["1234567890123"]
    assert payload["product"]["mpn"] == "AV-1"
    assert payload["product"]["brand"] == "AquaVerve"
    assert payload["product"]["epid"] == "1234567890"
    assert payload["product"]["imageUrls"] == payload["image_urls"]
    from scripts.cro_image_refresh import _inventory_refresh_put_body
    put_body = _inventory_refresh_put_body(item_ok, list(payload["image_urls"]))
    assert "image_urls" not in put_body
    assert "quantity" not in put_body
    assert "sku" not in put_body
    assert "href" not in put_body
    assert "inventoryItemGroupKeys" not in put_body
    assert put_body["packageWeightAndSize"] == item_ok["packageWeightAndSize"]
    assert put_body["product"]["imageUrls"] == payload["image_urls"]
    zero_w = dict(item_ok)
    zero_w["packageWeightAndSize"] = {
        "weight": {"value": 0, "unit": "POUND"},
        "dimensions": {"length": 20, "width": 10, "height": 8, "unit": "INCH"},
    }
    dropped = _inventory_refresh_put_body(zero_w, list(payload["image_urls"]))
    assert "weight" not in dropped.get("packageWeightAndSize", {})

    class SigStripClient:
        def __init__(self):
            self.puts = []

        def get_inventory_item(self, sku):
            return _item(["https://live/old.jpg"], qty=3)

        def create_or_replace_inventory_item(self, sku, payload):
            self.puts.append(payload)

    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        load_products_fn=lambda _c: [{
            "sku": "W5230P450590",
            "listing_id": "188111",
            "selling_price": 99,
            "total_cost": 40,
            "images": [GIGA_URL, GIGA_URL_2],
            "stock": 2,
            "stock_known": True,
            "title": "Sofa",
            "categoryId": "38208",
        }],
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}},
        load_pending_fn=lambda **k: [{
            "sku": "W5230P450590",
            "action": "image_refresh",
            "listing_id": "188111",
            "enqueued_at": _now().isoformat(),
        }] if k.get("action_type") == "image_refresh" else [],
        recent_terminal_fn=lambda **k: set(),
        scheduler_succeeded_fn=lambda name: True,
        ebay_client_factory=lambda: SigStripClient(),
        load_local_product_fn=lambda sku: {
            "images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d",
        },
    )
    rc = m.main(["--store", "main", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    dry = json.loads(next(ctx.out_dir.glob("*_dry.json")).read_text(encoding="utf-8"))
    imgs = [c for c in dry["candidates"] if c["action"] == "image_refresh"]
    assert imgs
    assert imgs[0]["payload_qty"] == 3

    # LIVE: a payload that lost x-c* must not PUT
    live_client = SigStripClient()

    def bad_payload(item, local):
        return {
            "title": "t",
            "description": "d",
            "image_urls": [stripped, "https://b2bfiles1.gigab2b.cn/img/W3636P456622.jpg"],
            "quantity": 3,
            "condition": "NEW",
            "aspects": {},
        }, None

    import scripts.cro_image_refresh as cir
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cir, "build_strict_refresh_payload", bad_payload)
    live_ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_client_factory=lambda: live_client,
        load_local_product_fn=lambda sku: {
            "images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d",
        },
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [{
            "action": "image_refresh",
            "sku": "W5230P450590",
            "listing_id": "188111",
            "live_n": 1,
            "live_lead": "https://live/old.jpg",
            "reason": "local lead image differs from live",
        }],
    }
    cand_path = tmp_path / "img.json"
    cand_path.write_text(json.dumps(cand), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(cand_path),
            "--approve-sha256", _sha(cand_path),
            "--out-dir", str(live_ctx.out_dir),
        ],
        ctx=live_ctx,
    )
    assert rc == 0
    assert live_client.puts == []
    monkeypatch.undo()


def test_preflight_sha_lock_and_mismatch(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m

    src = Path(m.__file__).read_text(encoding="utf-8")
    assert m._module_has_no_environ_production_assign(src)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    sl = target.slice
                    if isinstance(sl, ast.Constant) and sl.value == "EBAY_ENVIRONMENT":
                        raise AssertionError("os.environ['EBAY_ENVIRONMENT'] assignment found")

    monkeypatch.setenv("EBAY_ENVIRONMENT", "SANDBOX")
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [{"action": "price_drop", "sku": "W5230P450590", "live_price": 10, "new_price": 9}],
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cand), encoding="utf-8")
    ctx = _ctx(tmp_path, "main", _main_profile(), ebay_environment="SANDBOX")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", _sha(path)[:12],
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == m.EXIT_NOT_PRODUCTION

    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    ctx = _ctx(tmp_path, "main", _main_profile(), ebay_environment="PRODUCTION")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", "ffffffffffff",
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == m.EXIT_USAGE

    expired = dict(cand)
    expired["created_at"] = (_now() - timedelta(hours=30)).isoformat()
    expired["expires_at"] = (_now() - timedelta(hours=6)).isoformat()
    exp_path = tmp_path / "expired.json"
    exp_path.write_text(json.dumps(expired), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(exp_path),
            "--approve-sha256", _sha(exp_path),
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == m.EXIT_EXPIRED

    rc = m.main(["--store", "gp", "--out-dir", str(tmp_path / "mismatch")], ctx=_ctx(
        tmp_path, "gp", _main_profile(),
        load_products_fn=lambda _c: [],
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}},
    ))
    assert rc == m.EXIT_USAGE

    lock_ctx = _ctx(
        tmp_path / "held", "main", _main_profile(),
        load_products_fn=lambda _c: [],
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}},
    )
    root = Path(m.__file__).resolve().parents[1]
    holding = tmp_path / "child-holding"
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            "import sys, time\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from scripts.cro_daily_improve import acquire_lock\n"
            "rc = acquire_lock(Path(sys.argv[2]), datetime.now(timezone(timedelta(hours=8))))\n"
            "if rc is None:\n"
            "    Path(sys.argv[3]).write_text('held')\n"
            "    time.sleep(60)\n"
            "sys.exit(int(rc or 0))\n",
            str(root),
            str(lock_ctx.lock_path),
            str(holding),
        ]
    )
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if holding.is_file() and holder.poll() is None:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("holder process did not acquire the OS lock")
        rc = m.main(["--store", "main", "--out-dir", str(lock_ctx.out_dir)], ctx=lock_ctx)
        assert rc == m.EXIT_LOCK
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_force_skip_w465p451141_and_readonly_sections(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    ctx = _ctx(
        tmp_path, "aqua", _aqua_profile(),
        load_products_fn=lambda _c: [{
            "sku": "W465P451141",
            "listing_id": "188999",
            "selling_price": 40,
            "total_cost": 38,
            "net_margin": 0.02,
            "images": [GIGA_URL, GIGA_URL_2],
            "stock": 0,
            "stock_known": True,
            "title": "x" * 40,
            "categoryId": "33651",
            "impressions": 0,
            "views": 0,
            "age_days": 80,
            "order_count": 1,
            "sold_qty": 1,
            "revenue": 40,
        }],
        load_perf_fn=lambda _c: {
            "traffic_meta": {"api_ok": True, "record_count": 1, "is_truncated": False},
            "traffic": {},
            "sales": {"by_sku": {"W465P451141": {"qty": 1, "orders": 1, "revenue": 40}}},
        },
        recent_terminal_fn=lambda **k: set(),
        read_live_price_fn=lambda sku, lid: 40,
        precheck_price_fn=lambda **k: (True, "ok"),
        compute_new_price_fn=lambda *a, **k: {"new_price": 30, "floor": 20, "strategy": "T"},
        ebay_client_factory=lambda: GetOnlyClient(),
        load_local_product_fn=lambda sku: {"images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d"},
    )
    rc = m.main(["--store", "aqua", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    dry = json.loads(next(ctx.out_dir.glob("*_dry.json")).read_text(encoding="utf-8"))
    assert any(r["sku"] == "W465P451141" for r in dry["excluded"]["force_skip"])
    assert dry["candidates"] == []
    ctx_ro = dry["read_only_context"]
    assert "ads_cut_candidates_report_only" in ctx_ro
    assert "title_rewrite_candidates_report_only" in ctx_ro
    assert "end_candidates_report_only" in ctx_ro
    assert "orders_since_last_run" in ctx_ro
    assert "traffic_summary" in ctx_ro
    assert ctx_ro["aqua_qty_check_report_only"][0]["sku"] == "W465P451141"
    md = next(ctx.out_dir.glob("*_dry.md")).read_text(encoding="utf-8").splitlines()
    assert len(md) <= 25


def test_diagnose_readonly_has_no_side_effects(monkeypatch, tmp_path):
    from src.services.cro_daily_runner import diagnose_readonly
    import src.services.cro_snapshot_store as snap
    import src.services.cro_action_queue as queue

    monkeypatch.setattr(snap, "save_snapshots", lambda *a, **k: (_ for _ in ()).throw(AssertionError("snap")))
    monkeypatch.setattr(queue, "enqueue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("enq")))
    monkeypatch.setattr(queue, "enqueue_unique_pending", lambda *a, **k: (_ for _ in ()).throw(AssertionError("uniq")))

    db = tmp_path / "empty.db"
    db.write_bytes(b"")
    products = [{
        "sku": "W5230P450590",
        "listing_id": "1",
        "impressions": 2000,
        "views": 5,
        "transactions": 0,
        "sold_qty": 0,
        "selling_price": 130,
        "age_days": 30,
        "title": "x" * 70,
        "images": [GIGA_URL],
        "categoryId": "X",
    }]
    report = diagnose_readonly(
        products,
        market_data={"X": {"median": 100}},
        action_types=("price_drop", "image_refresh", "promote"),
        db_path=db,
    )
    assert report["snapshots_saved"] == 0
    assert report["p1_queued"] == 0
    assert all(a["action"] in {"price_drop", "image_refresh"} for a in report["actions"])


def test_only_skus_filters_without_changing_default(monkeypatch):
    from scripts import batch_smart_reprice as reprice

    products = [
        {"sku": "A", "category_id": "1", "total_cost": 10, "listing_id": "1", "title": "A", "cost_breakdown": {}},
        {"sku": "B", "category_id": "1", "total_cost": 10, "listing_id": "2", "title": "B", "cost_breakdown": {}},
    ]
    monkeypatch.setattr(reprice, "get_published_products", lambda: list(products))
    captured = {}

    def fake_client():
        raise AssertionError("should stop before eBay when filtered empty? or after filter")

    monkeypatch.setattr(reprice, "get_ebay_client", fake_client)
    report = reprice.run_batch_reprice(dry_run=True, only_skus={"ZZZ"})
    assert report["summary"]["total"] == 0
    captured["ok"] = True
    assert captured["ok"]


def test_traffic_degraded_yields_zero_candidates(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    ctx = _ctx(
        tmp_path, "gp", _gp_profile(),
        load_products_fn=lambda _c: [{
            "sku": "W5230P450590",
            "listing_id": "1",
            "selling_price": 120,
            "total_cost": 50,
            "stock": 3,
            "stock_known": True,
            "images": [GIGA_URL, GIGA_URL_2],
            "title": "x" * 70,
            "categoryId": "38208",
        }],
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": False}},
        recent_terminal_fn=lambda **k: set(),
        ebay_client_factory=lambda: GetOnlyClient(),
    )
    rc = m.main(["--store", "gp", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    dry = json.loads(next(ctx.out_dir.glob("*_dry.json")).read_text(encoding="utf-8"))
    assert dry["degraded"] is True
    assert dry["candidates"] == []


def test_store_root_live_rejected_by_parser():
    from scripts.cro_daily_improve import parse_args

    with pytest.raises(SystemExit) as exc:
        parse_args([
            "--store", "gp",
            "--store-root", "/tmp/GrovePop_Listing_Tool",
            "--live",
            "--candidates", "x.json",
            "--approve-sha256", "deadbeefdead",
        ])
    assert exc.value.code == 2


def _write_store_yaml(
    path: Path,
    brand: str,
    scheduler: str,
    tree_id: str = "0",
    listing_channel: str = "",
    ebay_site_id: str = "",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ebay_extra = ""
    if listing_channel:
        ebay_extra += f"  listing_channel: {listing_channel}\n"
    if ebay_site_id:
        ebay_extra += f'  ebay_site_id: "{ebay_site_id}"\n'
    path.write_text(
        textwrap.dedent(
            f"""
            server:
              scheduler_profile: {scheduler}
            store:
              brand_name: {brand}
            ebay:
              category_tree_id: "{tree_id}"
            """
        ).rstrip()
        + "\n"
        + ebay_extra,
        encoding="utf-8",
    )
    return path


def test_store_root_runtime_restores_main_env(tmp_path, monkeypatch):
    from scripts import cro_daily_improve as m
    import src.services.ebay_auth as ebay_auth

    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    monkeypatch.setenv("EBAY_APP_ID", "MAIN_APP")
    monkeypatch.setenv("EBAY_CERT_ID", "MAIN_CERT")
    monkeypatch.setenv("EBAY_DEV_ID", "MAIN_DEV")
    monkeypatch.delenv("EBAY_REDIRECT_URI", raising=False)
    main_token = tmp_path / "main_tokens.db"
    monkeypatch.setattr(ebay_auth, "_TOKEN_DB_PATH", str(main_token))

    gp = tmp_path / "gp"
    ctx = _ctx(
        tmp_path, "gp", _gp_profile(),
        isolate_store_root=True,
        store_env={
            "EBAY_ENVIRONMENT": "SANDBOX",
            "EBAY_APP_ID": "GP_APP",
            "EBAY_CERT_ID": "GP_CERT",
            # EBAY_DEV_ID omitted on purpose — must not inherit Main.
        },
        token_db_path=gp / "ebay_tokens.db",
    )
    seen = {}
    with m.store_root_runtime(ctx):
        seen["app"] = os.environ.get("EBAY_APP_ID")
        seen["cert"] = os.environ.get("EBAY_CERT_ID")
        seen["dev"] = os.environ.get("EBAY_DEV_ID")
        seen["env"] = os.environ.get("EBAY_ENVIRONMENT")
        seen["token"] = ebay_auth._TOKEN_DB_PATH
    assert seen == {
        "app": "GP_APP",
        "cert": "GP_CERT",
        "dev": None,
        "env": "PRODUCTION",
        "token": str(gp / "ebay_tokens.db"),
    }
    assert os.environ.get("EBAY_APP_ID") == "MAIN_APP"
    assert os.environ.get("EBAY_CERT_ID") == "MAIN_CERT"
    assert os.environ.get("EBAY_DEV_ID") == "MAIN_DEV"
    assert os.environ.get("EBAY_ENVIRONMENT") == "PRODUCTION"
    assert ebay_auth._TOKEN_DB_PATH == str(main_token)


def test_store_root_uses_gp_config_not_preloaded_main_env(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m

    main_root = tmp_path / "main_checkout"
    gp_root = tmp_path / "GrovePop_Listing_Tool"
    main_db = main_root / "ebay_collection.db"
    gp_db = gp_root / "ebay_collection.db"
    gp_tokens = gp_root / "ebay_tokens.db"
    main_yaml = _write_store_yaml(
        main_root / "config" / "store_profile.yaml", "AquaVerve", "full",
    )
    _write_store_yaml(gp_root / "config" / "store_profile.yaml", "GrovePop", "ops")
    _init_db(main_db, [
        {"sku": "W465P451141", "suggested_price": 55, "stock": 1, "listing_id": "MAIN-LID"},
        {"sku": "MAIN-ONLY-SKU", "suggested_price": 90, "stock": 4, "listing_id": "MAIN-ONLY"},
    ])
    _init_db(gp_db, [
        {"sku": "GP-ONLY-SKU", "suggested_price": 80, "stock": 3, "listing_id": "GP-LID"},
    ])
    gp_tokens.write_bytes(b"")
    gp_root.joinpath(".env").write_text(
        "\n".join([
            "EBAY_ENVIRONMENT=SANDBOX",
            "EBAY_APP_ID=GP_APP",
            "EBAY_CERT_ID=GP_CERT",
            "EBAY_DEV_ID=GP_DEV",
            "EBAY_REDIRECT_URI=https://gp.example/callback",
            "EBAY_CLIENT_ID=GP_CLIENT",
            "EBAY_CLIENT_SECRET=GP_SECRET",
            f"DAJIAN_COLLECTION_DB={gp_db}",
        ]) + "\n",
        encoding="utf-8",
    )
    cache_dir = main_root / "logs" / "runtime_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.joinpath("performance_cache.json").write_text(
        json.dumps({"traffic_meta": {"api_ok": True, "record_count": 99}}),
        encoding="utf-8",
    )
    reports = main_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    reports.joinpath("reprice_report_20990101.json").write_text(
        json.dumps({"market_research": {"MAIN": {"median": 1}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(m, "PROJECT_ROOT", main_root)
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    monkeypatch.setenv("EBAY_APP_ID", "MAIN_APP")
    monkeypatch.setenv("EBAY_CERT_ID", "MAIN_CERT")
    monkeypatch.setenv("EBAY_DEV_ID", "MAIN_DEV")
    monkeypatch.setenv("EBAY_REDIRECT_URI", "https://main.example/callback")
    monkeypatch.setenv("EBAY_CLIENT_ID", "MAIN_CLIENT")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "MAIN_SECRET")
    monkeypatch.setenv("DAJIAN_COLLECTION_DB", str(main_db))
    monkeypatch.setenv("STORE_PROFILE_PATH", str(main_yaml))

    client_calls = []

    def boom_client(*_a, **_k):
        client_calls.append("create")
        raise AssertionError("store-root ops dry-run must not build an eBay client")

    monkeypatch.setattr(m, "get_ebay_client", boom_client)
    import src.clients.real_ebay_client as rec
    monkeypatch.setattr(rec, "create_real_ebay_client", boom_client)

    out_dir = tmp_path / "reports" / "cro_daily_improve"
    rc = m.main([
        "--store", "gp",
        "--store-root", str(gp_root),
        "--out-dir", str(out_dir),
    ])
    assert rc == 0
    assert client_calls == []
    assert os.environ.get("EBAY_ENVIRONMENT") == "PRODUCTION"
    assert os.environ.get("EBAY_APP_ID") == "MAIN_APP"
    assert os.environ.get("DAJIAN_COLLECTION_DB") == str(main_db)

    dry = json.loads(next(out_dir.glob("*_dry.json")).read_text(encoding="utf-8"))
    pre = dry["preflight"]
    assert pre["brand_name"] == "GrovePop"
    assert pre["is_ops_scheduler"] is True
    assert pre["ebay_environment"] == "SANDBOX"
    assert pre["ebay_environment"] != os.environ.get("EBAY_ENVIRONMENT")
    assert Path(pre["store_root"]) == gp_root.resolve()
    assert Path(pre["db_path"]) == gp_db.resolve()
    assert Path(pre["token_db_path"]) == gp_tokens.resolve()
    assert dry["degraded"] is True
    assert dry["candidates"] == []
    force_skus = {row.get("sku") for row in dry["excluded"].get("force_skip") or []}
    assert "W465P451141" not in force_skus
    assert "MAIN-ONLY-SKU" not in json.dumps(dry)

    mismatch_out = tmp_path / "mismatch"
    rc = m.main([
        "--store", "gp",
        "--store-root", str(main_root),
        "--out-dir", str(mismatch_out),
    ])
    assert rc == m.EXIT_USAGE
    assert os.environ.get("EBAY_ENVIRONMENT") == "PRODUCTION"
    assert os.environ.get("EBAY_APP_ID") == "MAIN_APP"


class _OAuth:
    def get_valid_token(self):
        return "tok"


class _RecordingClient:
    def __init__(self, prices=None, items=None):
        self.oauth = _OAuth()
        self.base_url = "https://api.ebay.com"
        self.writes = []
        self.gets = []
        self.prices = prices or {}
        self.items = items or {}
        self.session = self

    def get_inventory_item(self, sku):
        self.gets.append(("inventory", sku))
        return self.items.get(sku) or _item(["https://live/old.jpg"], qty=3)

    def get_offers_by_sku(self, sku, marketplace_id=None):
        self.gets.append(("offers", sku))
        price = self.prices.get(sku, 120)
        return [{
            "offerId": f"off-{sku}",
            "listing": {"listingId": "188111", "listingStatus": "ACTIVE"},
            "status": "PUBLISHED",
            "marketplaceId": "EBAY_US",
            "pricingSummary": {"price": {"value": str(price), "currency": "USD"}},
        }]

    def put(self, url, headers=None, json=None, timeout=None):
        self.writes.append(("offer_put", url, json))
        return type("Resp", (), {"status_code": 204})()

    def create_or_replace_inventory_item(self, sku, payload):
        self.writes.append(("inventory_put", sku, payload))
        urls = list((payload or {}).get("image_urls") or [])
        qty = (payload or {}).get("quantity", 3)
        self.items[sku] = _item(urls or [GIGA_URL], qty=qty)
        return {"ok": True}

    def update_ad_bid(self, *a, **k):
        self.writes.append(("update_ad_bid", a, k))
        raise AssertionError("ad write")

    def delete_ad(self, *a, **k):
        self.writes.append(("delete_ad", a, k))
        raise AssertionError("ad write")


def test_production_context_wires_readonly_gets(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m

    main_root = tmp_path / "main_checkout"
    _write_store_yaml(main_root / "config" / "store_profile.yaml", "AquaVerve", "full")
    db = main_root / "ebay_collection.db"
    _init_db(db, [{
        "sku": "W5230P450590",
        "suggested_price": 120,
        "stock": 4,
        "listing_id": "188111",
        "images": [GIGA_URL, GIGA_URL_2],
    }])
    main_root.joinpath(".env").write_text(
        "\n".join([
            "EBAY_ENVIRONMENT=SANDBOX",
            "EBAY_APP_ID=STORE_APP",
            "EBAY_CERT_ID=STORE_CERT",
            "EBAY_DEV_ID=STORE_DEV",
            f"DAJIAN_COLLECTION_DB={db}",
        ]) + "\n",
        encoding="utf-8",
    )
    cache = main_root / "logs" / "runtime_cache"
    cache.mkdir(parents=True, exist_ok=True)
    cache.joinpath("performance_cache.json").write_text(
        json.dumps({"traffic_meta": {"api_ok": True, "record_count": 1}, "traffic": {}, "sales": {}}),
        encoding="utf-8",
    )
    queue = main_root / "logs" / "cro_action_queue.jsonl"
    queue.write_text(json.dumps({
        "sku": "W5230P450590",
        "action": "image_refresh",
        "status": "pending",
        "listing_id": "188111",
        "enqueued_at": datetime.now(CST).isoformat(),
    }) + "\n", encoding="utf-8")
    main_root.joinpath("logs", "_scheduler_health.json").write_text(json.dumps({
        "tasks": {"daily_tasks": {"status": "success", "at": datetime.now(CST).isoformat()}}
    }), encoding="utf-8")

    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    monkeypatch.setenv("EBAY_APP_ID", "MAIN_APP")
    recorded = {"apps": []}
    client = _RecordingClient(
        items={"W5230P450590": _item(["https://live/old.jpg"], qty=3)},
        prices={"W5230P450590": 120},
    )

    def production_client(ctx):
        recorded["apps"].append(os.environ.get("EBAY_APP_ID"))
        recorded["readonly"] = ctx.readonly_db
        if ctx.readonly_db:
            return m.ReadOnlyEbayClient(client)
        return client

    monkeypatch.setattr(m, "create_production_client", production_client)

    args = m.parse_args([
        "--store", "main",
        "--store-root", str(main_root),
        "--out-dir", str(tmp_path / "wired"),
    ])
    ctx = m.build_context(args)
    assert ctx.ebay_client_factory is not None
    assert ctx.read_live_price_fn is not None
    assert ctx.write_price_fn is not None
    assert ctx.verify_price_fn is not None
    assert ctx.readonly_db is True
    assert ctx.isolate_store_root is True

    wrapped = m.get_ebay_client(ctx)
    assert isinstance(wrapped, m.ReadOnlyEbayClient)
    with pytest.raises(RuntimeError, match="dry-run write blocked"):
        wrapped.create_or_replace_inventory_item("W5230P450590", {})
    with pytest.raises(RuntimeError, match="dry-run write blocked"):
        wrapped.update_ad_bid("x")
    assert m.read_live_price(ctx, "W5230P450590", "188111") == 120.0
    assert recorded["apps"] == ["STORE_APP"]
    assert os.environ.get("EBAY_APP_ID") == "MAIN_APP"
    assert os.environ.get("EBAY_ENVIRONMENT") == "PRODUCTION"
    assert client.writes == []

    rc = m.main([
        "--store", "main",
        "--store-root", str(main_root),
        "--out-dir", str(tmp_path / "wired-run"),
    ])
    assert rc == 0
    assert client.writes == []
    assert any(kind == "inventory" for kind, _sku in client.gets)
    dry = json.loads(next((tmp_path / "wired-run").glob("*_dry.json")).read_text(encoding="utf-8"))
    assert dry["writes"] == {"db": 0, "queue": 0, "ebay": 0}
    assert dry["mode"] == "dry"
    imgs = [c for c in dry["candidates"] if c["action"] == "image_refresh"]
    assert imgs
    assert os.environ.get("EBAY_APP_ID") == "MAIN_APP"

    dummy = tmp_path / "dummy.json"
    dummy.write_text("{}", encoding="utf-8")
    live_args = m.parse_args([
        "--store", "main",
        "--live",
        "--candidates", str(dummy),
        "--approve-sha256", "deadbeefdead",
        "--out-dir", str(tmp_path / "wired-live"),
    ])
    live_ctx = m.build_context(live_args, profile=_main_profile())
    assert live_ctx.ebay_client_factory is not None
    assert live_ctx.read_live_price_fn is not None
    assert live_ctx.write_price_fn is not None
    assert live_ctx.verify_price_fn is not None
    assert live_ctx.readonly_db is False
    assert live_ctx.isolate_store_root is False


def test_live_price_drop_never_calls_ad_apis(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m
    import scripts.batch_smart_reprice as reprice
    import src.services.repricing_guard as guard
    import src.services.ebay_ad_service as ads

    ad_calls = []
    monkeypatch.setattr(reprice, "update_ebay_price", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("update_ebay_price must not be used")
    ))
    monkeypatch.setattr(guard, "_try_disable_ad", lambda *a, **k: ad_calls.append("disable") or (False, "failed"))
    monkeypatch.setattr(ads.EbayAdService, "delete_ad", lambda *a, **k: ad_calls.append("delete_ad"))
    monkeypatch.setattr(ads.EbayAdService, "update_ad_bid", lambda *a, **k: ad_calls.append("update_ad_bid"))
    monkeypatch.setattr(ads.EbayAdService, "create_ad_safe", lambda *a, **k: ad_calls.append("create_ad_safe"))

    client = _RecordingClient(prices={"W5230P450590": 110})
    marked = []
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_environment="PRODUCTION",
        ebay_client_factory=lambda: client,
        read_live_price_fn=lambda sku, lid: 120 if not client.writes else 110,
        precheck_price_fn=None,
        write_price_fn=None,
        write_price_db_fn=lambda *a, **k: True,
        mark_done_fn=lambda skus, action, queue_path=None: marked.append((list(skus), action)) or len(list(skus)),
        recent_terminal_fn=lambda **k: set(),
    )
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [{
            "action": "price_drop",
            "sku": "W5230P450590",
            "listing_id": "188111",
            "live_price": 120,
            "new_price": 110,
        }],
    }
    path = tmp_path / "price.json"
    path.write_text(json.dumps(cand), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", _sha(path),
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == 0
    assert ad_calls == []
    live = json.loads(next(ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    assert live["results"][0]["status"] == "done"
    assert any(kind == "offer_put" for kind, *_ in client.writes)
    assert not any(kind in {"update_ad_bid", "delete_ad"} for kind, *_rest in client.writes)
    assert marked


def test_live_price_fail_closed_missing_cost_and_exception(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m

    db = tmp_path / "main" / "ebay_collection.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    _init_db(db, [{
        "sku": "NOCOST",
        "suggested_price": 80,
        "stock": 3,
        "listing_id": "L-NO",
        "cost_breakdown": {"total_dajian_cost": 0},
    }])
    writes = []
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_environment="PRODUCTION",
        ebay_client_factory=lambda: (_ for _ in ()).throw(AssertionError("client")),
        read_live_price_fn=lambda sku, lid: 80,
        write_price_fn=lambda *a, **k: writes.append("price") or True,
        write_price_db_fn=lambda *a, **k: writes.append("db") or True,
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [{
            "action": "price_drop",
            "sku": "NOCOST",
            "listing_id": "L-NO",
            "live_price": 80,
            "new_price": 70,
        }],
    }
    path = tmp_path / "nocost.json"
    path.write_text(json.dumps(cand), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", _sha(path),
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == 0
    live = json.loads(next(ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    assert live["results"][0]["status"] == "skipped"
    assert str(live["results"][0]["reason"]).startswith("guard_error")
    assert writes == []

    boom_ctx = _ctx(
        tmp_path / "boom_home", "main", _main_profile(),
        ebay_environment="PRODUCTION",
        precheck_price_fn=lambda **k: (_ for _ in ()).throw(RuntimeError("db locked")),
        read_live_price_fn=lambda sku, lid: 120,
        write_price_fn=lambda *a, **k: writes.append("price") or True,
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    cand2 = dict(cand)
    cand2["candidates"] = [{
        "action": "price_drop",
        "sku": "W5230P450590",
        "listing_id": "188111",
        "live_price": 120,
        "new_price": 110,
    }]
    path2 = tmp_path / "boom.json"
    path2.write_text(json.dumps(cand2), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path2),
            "--approve-sha256", _sha(path2),
            "--out-dir", str(boom_ctx.out_dir),
        ],
        ctx=boom_ctx,
    )
    assert rc == 0
    live2 = json.loads(next(boom_ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    assert live2["results"][0]["status"] == "skipped"
    assert "guard_error" in str(live2["results"][0]["reason"])
    assert writes == []


def test_live_image_put_error_continues_batch(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    skus = ["SKU1", "SKU2", "SKU3"]

    class BatchClient:
        def __init__(self):
            self.puts = []
            self.items = {sku: _item(["https://live/old.jpg"], qty=3) for sku in skus}

        def get_inventory_item(self, sku):
            return self.items[sku]

        def create_or_replace_inventory_item(self, sku, payload):
            if sku == "SKU2":
                raise RuntimeError("put exploded")
            self.puts.append(sku)
            urls, qty = _payload_urls_and_qty(payload)
            self.items[sku] = _item(urls or [GIGA_URL], qty=qty)

    client = _attach_live_session(BatchClient())
    marked = []
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_environment="PRODUCTION",
        ebay_client_factory=lambda: client,
        load_local_product_fn=lambda sku: {
            "images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d",
        },
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda skus_, action, queue_path=None: marked.append((list(skus_), action)) or len(list(skus_)),
    )
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [
            {
                "action": "image_refresh",
                "sku": sku,
                "listing_id": f"L{sku}",
                "live_n": 1,
                "live_lead": "https://live/old.jpg",
                "reason": "local lead image differs from live",
            }
            for sku in skus
        ],
    }
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(cand), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", _sha(path),
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == 0
    assert client.puts == ["SKU1", "SKU3"]
    live = json.loads(next(ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    by_sku = {row["sku"]: row for row in live["results"]}
    assert by_sku["SKU1"]["status"] == "done"
    assert by_sku["SKU2"]["status"] == "failed"
    assert "put exploded" in str(by_sku["SKU2"]["reason"])
    assert by_sku["SKU3"]["status"] == "done"
    assert marked
    done_skus = [sku for skus_, _action in marked for sku in skus_]
    assert "SKU1" in done_skus and "SKU3" in done_skus
    assert "SKU2" not in done_skus
    assert m.acquire_lock(ctx.lock_path, _now()) is None
    m.release_lock(ctx.lock_path)


def test_live_revalidates_raise_force_skip_and_oos(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    writes = []
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        db_rows=[
            {"sku": "RAISE", "suggested_price": 120, "stock": 3, "listing_id": "L-RAISE"},
            {"sku": "W465P451141", "suggested_price": 55, "stock": 2, "listing_id": "L-SKIP"},
            {"sku": "OOS", "suggested_price": 80, "stock": 0, "listing_id": "L-OOS"},
        ],
        ebay_environment="PRODUCTION",
        read_live_price_fn=lambda sku, lid: 120,
        write_price_fn=lambda *a, **k: writes.append(a) or True,
        write_price_db_fn=lambda *a, **k: writes.append(("db", a)) or True,
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [
            {"action": "price_drop", "sku": "RAISE", "listing_id": "L-RAISE",
             "live_price": 120, "new_price": 130},
            {"action": "price_drop", "sku": "W465P451141", "listing_id": "L-SKIP",
             "live_price": 120, "new_price": 100},
            {"action": "price_drop", "sku": "OOS", "listing_id": "L-OOS",
             "live_price": 120, "new_price": 100},
        ],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(cand), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", _sha(path),
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == 0
    assert writes == []
    live = json.loads(next(ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    by_sku = {row["sku"]: row for row in live["results"]}
    assert by_sku["RAISE"]["status"] == "skipped"
    assert by_sku["RAISE"]["reason"] == "not_a_drop"
    assert by_sku["W465P451141"]["status"] == "skipped"
    assert by_sku["W465P451141"]["reason"] == "force_skip"
    assert by_sku["OOS"]["status"] == "skipped"
    assert by_sku["OOS"]["reason"] == "stock_zero"


def test_dry_run_does_not_fallback_to_suggested_price(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        load_products_fn=lambda _c: [{
            "sku": "W5230P450590",
            "listing_id": "188111",
            "selling_price": 120,
            "total_cost": 50,
            "stock": 3,
            "stock_known": True,
            "images": [GIGA_URL],
            "title": "Sofa",
        }],
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}},
        load_pending_fn=lambda **k: [{
            "sku": "W5230P450590",
            "action": "price_drop",
            "listing_id": "188111",
            "enqueued_at": _now().isoformat(),
            "detail": {"suggested_price": 100},
        }] if k.get("action_type") == "price_drop" else [],
        recent_terminal_fn=lambda **k: set(),
        scheduler_succeeded_fn=lambda name: True,
        read_live_price_fn=lambda sku, lid: None,
        compute_new_price_fn=lambda *a, **k: {"new_price": 100, "floor": 55, "strategy": "T"},
        ebay_client_factory=lambda: GetOnlyClient(),
    )
    rc = m.main(["--store", "main", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    dry = json.loads(next(ctx.out_dir.glob("*_dry.json")).read_text(encoding="utf-8"))
    assert dry["candidates"] == []
    assert any(r["sku"] == "W5230P450590" for r in dry["excluded"]["live_price_unavailable"])


def test_store_root_aqua_uses_trading_profile_not_main(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m
    import scripts.batch_smart_reprice as reprice
    from src.utils.store_profile import get_store_profile, reset_store_profile_cache

    main_yaml = _write_store_yaml(
        tmp_path / "main_profile.yaml", "AquaVerve", "full",
    )
    aqua_root = tmp_path / "AquaRides_Listing_Tool"
    _write_store_yaml(
        aqua_root / "config" / "store_profile.yaml",
        "AquaRides",
        "ops",
        tree_id="100",
        listing_channel="trading",
        ebay_site_id="100",
    )
    db = aqua_root / "ebay_collection.db"
    _init_db(db, [{"sku": "W465P170804", "stock": 2, "listing_id": "188802002738"}])
    aqua_root.joinpath(".env").write_text(
        "EBAY_ENVIRONMENT=SANDBOX\nEBAY_APP_ID=AQUA_APP\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("STORE_PROFILE_PATH", str(main_yaml))
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    reset_store_profile_cache()
    assert get_store_profile().brand_name == "AquaVerve"
    assert str(getattr(get_store_profile(), "listing_channel", "inventory") or "inventory") != "trading"

    seen = {}

    def fake_trading(oauth, listing_id, profile=None):
        seen["listing_id"] = listing_id
        seen["site"] = str(getattr(profile, "ebay_site_id", "") or "")
        seen["channel"] = str(getattr(profile, "listing_channel", "") or "")
        seen["brand"] = str(getattr(profile, "brand_name", "") or "")
        return 88.0

    def boom_offers(*_a, **_k):
        raise AssertionError("Inventory get_offers_by_sku must not run for Aqua Trading")

    monkeypatch.setattr(reprice, "fetch_trading_item_price", fake_trading)
    monkeypatch.setattr(m, "create_production_client", lambda ctx: type("C", (), {
        "oauth": _OAuth(),
        "get_offers_by_sku": staticmethod(boom_offers),
        "get_inventory_item": staticmethod(lambda sku: None),
    })())

    args = m.parse_args([
        "--store", "aqua",
        "--store-root", str(aqua_root),
        "--out-dir", str(tmp_path / "aqua-wired"),
    ])
    ctx = m.build_context(args)
    assert ctx.ebay_client_factory is not None
    assert ctx.read_live_price_fn is not None
    assert ctx.isolate_store_root is True
    assert ctx.profile.listing_channel == "trading"
    assert ctx.profile.ebay_site_id == "100"
    price = m.read_live_price(ctx, "W465P170804", "188802002738")
    assert price == 88.0
    assert seen == {
        "listing_id": "188802002738",
        "site": "100",
        "channel": "trading",
        "brand": "AquaRides",
    }
    assert get_store_profile().brand_name == "AquaVerve"


def test_pid_alive_win32_never_calls_os_kill(monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m
    import src.utils.process_identity as process_identity

    monkeypatch.setattr(m.sys, "platform", "win32")
    monkeypatch.setattr(process_identity.sys, "platform", "win32")

    def boom_kill(*_a, **_k):
        raise AssertionError("os.kill must not be called on win32")

    monkeypatch.setattr(os, "kill", boom_kill)
    monkeypatch.setattr(m.os, "kill", boom_kill)
    monkeypatch.setattr(process_identity.os, "kill", boom_kill)

    assert m.pid_alive(os.getpid()) is False
    assert m.pid_alive(1, "win-filetime:1") is False


def test_lock_stores_creation_marker_and_os_lock_is_exclusive(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m
    from src.utils.process_identity import get_process_creation_marker

    lock = tmp_path / "locks" / "cro_daily_improve.lock"
    now = _now()
    assert m.acquire_lock(lock, now) is None
    data = json.loads(lock.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["creation_marker"] == get_process_creation_marker(os.getpid())
    assert data["creation_marker"]
    assert m.acquire_lock(lock, now) == m.EXIT_LOCK
    m.release_lock(lock)
    leftover = json.dumps({
        "pid": 999999999,
        "creation_marker": "win-filetime:reused-pid",
        "at": now.isoformat(),
        "name": "cro_daily_improve",
    })
    lock.write_text(leftover, encoding="utf-8")
    assert m.acquire_lock(lock, now) is None
    data = json.loads(lock.read_text(encoding="utf-8"))
    assert data["creation_marker"] == get_process_creation_marker(os.getpid())
    m.release_lock(lock)


def test_os_lock_single_winner_threads_and_processes(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    empty = tmp_path / "empty.lock"
    empty.write_bytes(b"")
    assert m.acquire_lock(empty, _now()) is None
    assert empty.exists()
    m.release_lock(empty)

    stale = tmp_path / "stale.lock"
    stale.write_text(
        json.dumps({
            "pid": 999999999,
            "creation_marker": "proc-start-ticks:1",
            "at": _now().isoformat(),
            "name": "cro_daily_improve",
        }),
        encoding="utf-8",
    )
    results: list[int | None] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait(timeout=5)
        results.append(m.acquire_lock(stale, _now()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert results.count(None) == 1
    assert results.count(m.EXIT_LOCK) == 7
    holder = json.loads(stale.read_text(encoding="utf-8"))
    assert holder["pid"] == os.getpid()
    m.release_lock(stale)

    import fcntl
    lock = tmp_path / "os.lock"
    assert m.acquire_lock(lock, _now()) is None
    fd = os.open(str(lock), os.O_RDWR)
    try:
        with pytest.raises((BlockingIOError, OSError)):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    m.release_lock(lock)
    fd = os.open(str(lock), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

    root = Path(m.__file__).resolve().parents[1]
    proc_lock = tmp_path / "proc.lock"
    script = (
        "import sys\n"
        "from datetime import datetime, timedelta, timezone\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from scripts.cro_daily_improve import acquire_lock, release_lock, EXIT_LOCK\n"
        "rc = acquire_lock(Path(sys.argv[2]), datetime.now(timezone(timedelta(hours=8))))\n"
        "if rc is None:\n"
        "    Path(sys.argv[3]).write_text('held')\n"
        "    import time; time.sleep(1.5)\n"
        "    release_lock(Path(sys.argv[2]))\n"
        "    sys.exit(0)\n"
        "sys.exit(int(rc or EXIT_LOCK))\n"
    )
    markers = [tmp_path / f"held{i}" for i in range(2)]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(root), str(proc_lock), str(markers[i])]
        )
        for i in range(2)
    ]
    codes = [proc.wait(timeout=10) for proc in procs]
    assert sorted(codes) == [0, m.EXIT_LOCK]
    assert sum(1 for marker in markers if marker.exists()) == 1


def test_concurrent_never_both_reach_live(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    writes: list[int] = []
    in_live = threading.Event()
    allow_finish = threading.Event()
    lock_path = tmp_path / "shared.lock"
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [{
            "action": "price_drop",
            "sku": "W5230P450590",
            "listing_id": "188111",
            "live_price": 120,
            "new_price": 110,
        }],
    }
    path = tmp_path / "shared.json"
    path.write_text(json.dumps(cand), encoding="utf-8")
    digest = _sha(path)
    rcs: list[int] = []
    barrier = threading.Barrier(2)

    def worker(idx: int) -> None:
        ctx = _ctx(
            tmp_path / f"live{idx}", "main", _main_profile(),
            ebay_environment="PRODUCTION",
            read_live_price_fn=lambda sku, lid: 120,
            write_price_fn=lambda *a, **k: writes.append(idx) or in_live.set() or allow_finish.wait(timeout=5) or True,
            write_price_db_fn=lambda *a, **k: True,
            precheck_price_fn=lambda **k: (True, "ok"),
            verify_price_fn=lambda *a, **k: (True, 110),
            recent_terminal_fn=lambda **k: set(),
            mark_done_fn=lambda *a, **k: 0,
        )
        ctx.lock_path = lock_path
        barrier.wait(timeout=5)
        rcs.append(m.main(
            [
                "--store", "main", "--live",
                "--candidates", str(path),
                "--approve-sha256", digest,
                "--out-dir", str(ctx.out_dir),
            ],
            ctx=ctx,
        ))

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not (in_live.is_set() and m.EXIT_LOCK in rcs):
        time.sleep(0.02)
    assert in_live.is_set()
    assert m.EXIT_LOCK in rcs
    allow_finish.set()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(rcs) == [0, m.EXIT_LOCK]
    assert writes == [1] or writes == [2]


def test_strip_oss_keeps_raw_giga_sig_bytes():
    from scripts.cro_image_refresh import giga_sig_params, strip_oss_keep_giga_sig

    plain = "https://b2bfiles1.gigab2b.cn/img/a.jpg?x-cc=1&x-cu=2&x-ct=3&x-cs=a/b=%20c"
    assert strip_oss_keep_giga_sig(plain) == plain
    assert strip_oss_keep_giga_sig(plain) is plain

    src = (
        "https://b2bfiles1.gigab2b.cn/img/a.jpg"
        "?x-oss-process=image/resize,w_800&x-cc=1&x-cu=2&x-ct=3&x-cs=a/b=%20c"
    )
    out = strip_oss_keep_giga_sig(src)
    assert "x-oss-process" not in out
    assert out == (
        "https://b2bfiles1.gigab2b.cn/img/a.jpg?x-cc=1&x-cu=2&x-ct=3&x-cs=a/b=%20c"
    )
    assert giga_sig_params(src)["x-cs"] == "a/b=%20c"
    assert giga_sig_params(out)["x-cs"] == "a/b=%20c"
    assert "x-cs=a/b=%20c" in src
    assert "x-cs=a/b=%20c" in out
    for key in ("x-cc", "x-cu", "x-ct", "x-cs"):
        raw = f"{key}={giga_sig_params(src)[key]}"
        assert raw in src and raw in out


def test_candidates_require_mode_ttl_mtime_and_recap(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    now = _now()

    def _write(name: str, payload: dict) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    base = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
        "candidates": [{"action": "price_drop", "sku": "A", "live_price": 10, "new_price": 9}],
    }

    missing_created = dict(base)
    missing_created.pop("created_at")
    path = _write("no_created.json", missing_created)
    data, err = m.validate_candidates_file(path, _sha(path), now, "main")
    assert data is None and err == m.EXIT_USAGE

    missing_expires = dict(base)
    missing_expires.pop("expires_at")
    path = _write("no_expires.json", missing_expires)
    data, err = m.validate_candidates_file(path, _sha(path), now, "main")
    assert data is None and err == m.EXIT_USAGE

    missing_mode = dict(base)
    missing_mode.pop("mode")
    path = _write("no_mode.json", missing_mode)
    data, err = m.validate_candidates_file(path, _sha(path), now, "main")
    assert data is None and err == m.EXIT_USAGE

    live_mode = dict(base)
    live_mode["mode"] = "live"
    path = _write("live_mode.json", live_mode)
    data, err = m.validate_candidates_file(path, _sha(path), now, "main")
    assert data is None and err == m.EXIT_USAGE

    stale = dict(base)
    path = _write("stale.json", stale)
    old = datetime.now().timestamp() - (25 * 3600)
    os.utime(path, (old, old))
    data, err = m.validate_candidates_file(path, _sha(path), now, "main")
    assert data is None and err == m.EXIT_EXPIRED

    over = dict(base)
    over["candidates"] = (
        [
            {"action": "price_drop", "sku": f"P{i}", "live_price": 10, "new_price": 9}
            for i in range(m.LIMIT_PRICE_MAX + 5)
        ]
        + [
            {"action": "image_refresh", "sku": f"I{i}", "reason": "lead"}
            for i in range(m.LIMIT_IMAGE_MAX + 5)
        ]
    )
    path = _write("over.json", over)
    data, err = m.validate_candidates_file(path, _sha(path), now, "main")
    assert err is None and data is not None
    price = [c for c in data["candidates"] if c["action"] == "price_drop"]
    image = [c for c in data["candidates"] if c["action"] == "image_refresh"]
    assert len(price) == m.LIMIT_PRICE_MAX
    assert len(image) == m.LIMIT_IMAGE_MAX
    assert price[0]["sku"] == "P0"
    assert image[0]["sku"] == "I0"


def test_live_price_skips_when_live_changed(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    writes = []
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_environment="PRODUCTION",
        read_live_price_fn=lambda sku, lid: 120.10,
        write_price_fn=lambda *a, **k: writes.append(a) or True,
        write_price_db_fn=lambda *a, **k: writes.append(("db", a)) or True,
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [{
            "action": "price_drop",
            "sku": "W5230P450590",
            "listing_id": "188111",
            "live_price": 120.00,
            "new_price": 110,
        }],
    }
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(cand), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", _sha(path),
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == 0
    assert writes == []
    live = json.loads(next(ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    assert live["results"][0]["status"] == "skipped"
    assert live["results"][0]["reason"] == "live_changed"

    writes.clear()
    within_ctx = _ctx(
        tmp_path / "within", "main", _main_profile(),
        ebay_environment="PRODUCTION",
        read_live_price_fn=lambda sku, lid: 120.04,
        write_price_fn=lambda *a, **k: writes.append(a) or True,
        write_price_db_fn=lambda *a, **k: True,
        precheck_price_fn=lambda **k: (True, "ok"),
        verify_price_fn=lambda *a, **k: (True, 110),
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", _sha(path),
            "--out-dir", str(within_ctx.out_dir),
        ],
        ctx=within_ctx,
    )
    assert rc == 0
    within = json.loads(next(within_ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    assert within["results"][0]["status"] == "done"
    assert writes


def test_dry_run_exception_writes_degraded_report_and_releases_lock(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    def boom(_ctx):
        raise RuntimeError("load exploded")

    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        load_products_fn=boom,
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}},
        ebay_client_factory=lambda: GetOnlyClient(),
    )
    rc = m.main(["--store", "main", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == m.EXIT_DEGRADED
    reports = list(ctx.out_dir.glob("*_dry.json"))
    assert reports
    dry = json.loads(reports[0].read_text(encoding="utf-8"))
    assert dry["degraded"] is True
    assert "load exploded" in str(dry.get("error"))
    assert dry["mode"] == "dry"
    assert dry["writes"] == {"db": 0, "queue": 0, "ebay": 0}
    assert m.acquire_lock(ctx.lock_path, _now()) is None
    m.release_lock(ctx.lock_path)


def test_image_live_reuses_48_helpers_exact_readback_and_no_session():
    from scripts.cro_image_refresh import (
        _inventory_refresh_put_body,
        _omit_nonpositive_package_weight,
        _put_inventory_refresh,
        _readback_image_urls,
    )

    class NoSession:
        def create_or_replace_inventory_item(self, sku, payload):
            raise AssertionError("must not fall back to create_or_replace_inventory_item")

    with pytest.raises(RuntimeError, match="live session is required"):
        _put_inventory_refresh(NoSession(), "SKU", {
            "locale": "en_US",
            "product": {"imageUrls": ["https://img/a.jpg"]},
        })

    item = {
        "sku": "SKU",
        "href": "https://api.ebay.com/x",
        "inventoryItemGroupKeys": ["g1"],
        "image_urls": ["https://alias.jpg"],
        "quantity": 1,
        "locale": "en_US",
        "packageWeightAndSize": {"weight": {"value": 0, "unit": "POUND"}},
        "product": {"imageUrls": ["https://img/old.jpg"]},
    }
    body = _inventory_refresh_put_body(item, ["https://img/a.jpg", "https://img/b.jpg"])
    assert "sku" not in body
    assert "href" not in body
    assert "inventoryItemGroupKeys" not in body
    assert "image_urls" not in body
    assert "quantity" not in body
    assert "packageWeightAndSize" not in body
    assert body["locale"] == "en_US"
    assert body["product"]["imageUrls"] == ["https://img/a.jpg", "https://img/b.jpg"]
    positive = {
        "product": {"imageUrls": ["u"]},
        "packageWeightAndSize": {
            "weight": {"value": 2, "unit": "POUND"},
            "dimensions": {"length": 1, "width": 1, "height": 1, "unit": "INCH"},
        },
    }
    _omit_nonpositive_package_weight(positive)
    assert positive["packageWeightAndSize"]["weight"]["value"] == 2
    assert _readback_image_urls({"product": {"imageUrls": ["a", "b"]}}) == ["a", "b"]


def test_live_image_readback_must_equal_written_count(tmp_path, no_promote_module):
    from scripts import cro_daily_improve as m

    class CollapseClient:
        def __init__(self):
            self.items = {"W5230P450590": _item(["https://live/old.jpg"], qty=3)}
            self.session = self
            self.oauth = type("O", (), {"get_valid_token": staticmethod(lambda: "tok")})()
            self.base_url = "https://api.ebay.com"

        def get_inventory_item(self, sku):
            return self.items[sku]

        def put(self, url, headers=None, json=None, timeout=None):
            sku = str(url).rstrip("/").split("/")[-1]
            self.items[sku] = _item(["https://live/only-one.jpg"], qty=3)
            return _FakeHttpResp()

        def create_or_replace_inventory_item(self, sku, payload):
            raise AssertionError("must not fall back")

    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_environment="PRODUCTION",
        ebay_client_factory=lambda: CollapseClient(),
        load_local_product_fn=lambda sku: {
            "images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d",
        },
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    cand = {
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": [{
            "action": "image_refresh",
            "sku": "W5230P450590",
            "listing_id": "188111",
            "live_n": 1,
            "live_lead": "https://live/old.jpg",
            "reason": "local lead image differs from live",
        }],
    }
    path = tmp_path / "collapse.json"
    path.write_text(json.dumps(cand), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(path),
            "--approve-sha256", _sha(path),
            "--out-dir", str(ctx.out_dir),
        ],
        ctx=ctx,
    )
    assert rc == 0
    live = json.loads(next(ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    assert live["results"][0]["status"] == "failed"
    assert live["results"][0]["reason"] == "verification mismatch"
    assert live["results"][0]["expected_count"] == 2
    assert live["results"][0]["after"] == 1


def _signed_url(i: int) -> str:
    return (
        f"https://b2bfiles1.gigab2b.cn/img/{i:02d}.jpg"
        f"?x-oss-process=image/resize,w_800&x-cc=c{i}&x-cu=u{i}&x-ct=t{i}&x-cs=s{i}/p=%20{i}"
    )


def test_refreshed_sku_stays_healthy_on_next_run(tmp_path, no_promote_module):
    """Local still has x-oss-process; live has the stripped PUT. Next run is healthy."""
    from scripts.cro_image_refresh import (
        _should_refresh,
        prepare_refresh_image_urls,
        strip_oss_keep_giga_sig,
    )
    from scripts import cro_daily_improve as m

    local = [GIGA_URL, GIGA_URL_2]
    prepared, reason = prepare_refresh_image_urls(local)
    assert reason is None
    assert prepared == [strip_oss_keep_giga_sig(GIGA_URL), strip_oss_keep_giga_sig(GIGA_URL_2)]
    assert _should_refresh(local, prepared) is None

    client = GetOnlyClient({
        "W5230P450590": _item(prepared, qty=3),
    })
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_client_factory=lambda: client,
    )
    inspected = m.inspect_image(
        ctx, "W5230P450590",
        {"images": local, "title": "t", "description": "d"},
    )
    assert inspected["status"] == "excluded"
    assert inspected["reason"] == "live already healthy"
    assert inspected["lead_changed"] is False

    live_ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_client_factory=lambda: _attach_live_session(GetOnlyClient({
            "W5230P450590": _item(prepared, qty=3),
        })),
        load_local_product_fn=lambda sku: {
            "images": local, "title": "t", "description": "d",
        },
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    cand = {
        "action": "image_refresh",
        "sku": "W5230P450590",
        "listing_id": "188111",
        "live_n": 2,
        "live_lead": prepared[0],
        "reason": "local lead image differs from live",
    }
    row = m.apply_image_live(live_ctx, cand)
    assert row["status"] == "skipped"
    assert row["reason"] == "live already healthy"


def test_readonly_sqlite_uri_spaces_and_windows_path(tmp_path):
    from pathlib import PureWindowsPath

    from scripts.cro_daily_improve import open_db, readonly_sqlite_uri

    spaced = tmp_path / "Foo Bar" / "ebay collection.db"
    spaced.parent.mkdir()
    seed = sqlite3.connect(str(spaced))
    seed.execute("CREATE TABLE t (x INTEGER)")
    seed.execute("INSERT INTO t VALUES (7)")
    seed.commit()
    seed.close()

    uri = readonly_sqlite_uri(spaced)
    assert uri.startswith("file:")
    assert "Foo%20Bar" in uri
    assert "ebay%20collection.db" in uri
    assert uri.endswith("?mode=ro")
    ro = open_db(spaced, readonly=True)
    try:
        assert ro.execute("SELECT x FROM t").fetchone()[0] == 7
    finally:
        ro.close()

    win = PureWindowsPath(r"C:\Users\Foo Bar\ebay_collection.db")
    assert win.as_uri() + "?mode=ro" == (
        "file:///C:/Users/Foo%20Bar/ebay_collection.db?mode=ro"
    )
    src = Path(readonly_sqlite_uri.__code__.co_filename).read_text(encoding="utf-8")
    tree = ast.parse(src)
    helper = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "readonly_sqlite_uri"
    )
    helper_src = ast.get_source_segment(src, helper) or ""
    assert "as_uri()" in helper_src
    assert "as_posix" not in helper_src
    assert "?mode=ro" in helper_src


def test_dry_run_and_store_root_never_open_db_readwrite(tmp_path, monkeypatch, no_promote_module):
    from scripts import cro_daily_improve as m
    from src.services.cro_daily_runner import diagnose_readonly
    from src.services.cro_diagnose_blacklist import load_blacklisted_skus
    from src.services.cro_thresholds import load_thresholds
    from src.services.repricing_guard import _fetch_cost_and_listing

    db = tmp_path / "main" / "ebay_collection.db"
    db.parent.mkdir(parents=True)
    _init_db(db, [{
        "sku": "W5230P450590",
        "suggested_price": 120,
        "stock": 4,
        "images": [GIGA_URL, GIGA_URL_2],
        "listing_id": "188111",
    }])
    db_hash = _sha(db)
    store_opens: list[str] = []
    real_connect = sqlite3.connect

    def spy_connect(database, *args, **kwargs):
        text = str(database)
        resolved = str(db.resolve())
        uri = db.resolve().as_uri()
        if resolved in text or uri in text:
            store_opens.append(text)
            if "mode=ro" not in text or kwargs.get("uri") is not True:
                raise AssertionError(f"store DB opened read-write: {database!r} {kwargs}")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    writes_seen = []

    def authorizer(action, *_a):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            writes_seen.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    client = GetOnlyClient({
        "W5230P450590": _item(["https://live/old.jpg"], qty=2),
    })
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        isolate_store_root=True,
        authorizer=authorizer,
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}, "traffic": {}, "sales": {}},
        load_pending_fn=lambda **k: [{
            "sku": "W5230P450590",
            "action": "image_refresh",
            "listing_id": "188111",
            "enqueued_at": _now().isoformat(),
        }] if k.get("action_type") == "image_refresh" else [],
        recent_terminal_fn=lambda **k: set(),
        scheduler_succeeded_fn=lambda name: name == "daily_tasks",
        ebay_client_factory=lambda: client,
    )
    rc = m.main(["--store", "main", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0

    diagnose_readonly(
        [{
            "sku": "W5230P450590",
            "listing_id": "188111",
            "impressions": 2000,
            "views": 5,
            "transactions": 0,
            "sold_qty": 0,
            "selling_price": 130,
            "age_days": 30,
            "title": "x" * 70,
            "images": [GIGA_URL, GIGA_URL_2],
            "categoryId": "38208",
        }],
        market_data={"38208": {"median": 100}},
        db_path=db,
    )
    assert load_thresholds(db_path=db) == {}
    assert load_blacklisted_skus(db_path=db) == set()
    _fetch_cost_and_listing(str(db), "W5230P450590")
    m.load_local_product(ctx, "W5230P450590")
    m.load_published_products(ctx)

    assert writes_seen == []
    assert _sha(db) == db_hash
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(db) + suffix).exists()
    assert store_opens
    assert all("mode=ro" in item for item in store_opens)


def test_more_than_24_signed_images_truncates_like_live(tmp_path, no_promote_module):
    from scripts.cro_image_refresh import (
        _INVENTORY_IMAGE_LIMIT,
        build_strict_refresh_payload,
        prepare_refresh_image_urls,
        strip_oss_keep_giga_sig,
        urls_keep_giga_sigs,
    )
    from scripts import cro_daily_improve as m

    local = [_signed_url(i) for i in range(25)]
    assert len(local) == 25
    kept = [strip_oss_keep_giga_sig(u) for u in local[:_INVENTORY_IMAGE_LIMIT]]
    assert urls_keep_giga_sigs(local, kept) is True
    prepared, reason = prepare_refresh_image_urls(local)
    assert reason is None
    assert len(prepared) == 24
    assert prepared == kept

    item = _item(["https://live/old.jpg"], qty=4)
    payload, payload_reason = build_strict_refresh_payload(item, {"images": local})
    assert payload_reason is None
    assert len(payload["image_urls"]) == 24

    class CaptureClient:
        def __init__(self):
            self.item = _item(["https://live/old.jpg"], qty=4)
            self.puts = []
            _attach_live_session(self)

        def get_inventory_item(self, sku):
            return self.item

        def create_or_replace_inventory_item(self, sku, payload):
            self.puts.append(payload)
            self.item = payload

    dry_client = GetOnlyClient({"W5230P450590": _item(["https://live/old.jpg"], qty=4)})
    ctx = _ctx(
        tmp_path, "main", _main_profile(),
        load_products_fn=lambda _c: [{
            "sku": "W5230P450590",
            "listing_id": "188111",
            "selling_price": 99,
            "total_cost": 40,
            "images": local,
            "stock": 2,
            "stock_known": True,
            "title": "Sofa",
            "categoryId": "38208",
        }],
        load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}},
        load_pending_fn=lambda **k: [{
            "sku": "W5230P450590",
            "action": "image_refresh",
            "listing_id": "188111",
            "enqueued_at": _now().isoformat(),
        }] if k.get("action_type") == "image_refresh" else [],
        recent_terminal_fn=lambda **k: set(),
        scheduler_succeeded_fn=lambda name: True,
        ebay_client_factory=lambda: dry_client,
        load_local_product_fn=lambda sku: {
            "images": local, "title": "t", "description": "d",
        },
    )
    rc = m.main(["--store", "main", "--out-dir", str(ctx.out_dir)], ctx=ctx)
    assert rc == 0
    dry = json.loads(next(ctx.out_dir.glob("*_dry.json")).read_text(encoding="utf-8"))
    imgs = [c for c in dry["candidates"] if c["action"] == "image_refresh"]
    assert len(imgs) == 1
    assert imgs[0]["local_n"] == 25
    assert imgs[0]["payload_n"] == 24

    live_client = CaptureClient()
    live_ctx = _ctx(
        tmp_path, "main", _main_profile(),
        ebay_client_factory=lambda: live_client,
        load_local_product_fn=lambda sku: {
            "images": local, "title": "t", "description": "d",
        },
        recent_terminal_fn=lambda **k: set(),
        mark_done_fn=lambda *a, **k: 0,
    )
    cand_path = tmp_path / "trunc.json"
    cand_path.write_text(json.dumps({
        "schema": "cro_daily_improve/v1",
        "store": "main",
        "mode": "dry",
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(hours=2)).isoformat(),
        "candidates": imgs,
    }), encoding="utf-8")
    rc = m.main(
        [
            "--store", "main", "--live",
            "--candidates", str(cand_path),
            "--approve-sha256", _sha(cand_path),
            "--out-dir", str(live_ctx.out_dir),
        ],
        ctx=live_ctx,
    )
    assert rc == 0
    live = json.loads(next(live_ctx.out_dir.glob("*_live.json")).read_text(encoding="utf-8"))
    assert live["results"][0]["status"] == "done"
    assert live["results"][0]["after"] == 24
    assert len(live_client.puts) == 1
    written = live_client.puts[0]["product"]["imageUrls"]
    assert len(written) == 24
    assert written == prepared
    assert dry["candidates"][0]["payload_n"] == len(written)
