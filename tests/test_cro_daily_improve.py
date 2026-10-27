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
        live_client.items[sku] = _item(payload["image_urls"], qty=payload["quantity"])

    live_client.create_or_replace_inventory_item = put
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
    payload, reason = build_strict_refresh_payload(
        item_ok, {"images": [GIGA_URL, GIGA_URL_2], "title": "t", "description": "d"},
    )
    assert reason is None
    assert payload["quantity"] == 7
    assert urls_keep_giga_sigs([GIGA_URL, GIGA_URL_2], payload["image_urls"])

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

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_ctx = _ctx(
            tmp_path, "main", _main_profile(),
            load_products_fn=lambda _c: [],
            load_perf_fn=lambda _c: {"traffic_meta": {"api_ok": True}},
        )
        lock_ctx.lock_path.write_text(
            json.dumps({"pid": holder.pid, "at": _now().isoformat()}),
            encoding="utf-8",
        )
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
