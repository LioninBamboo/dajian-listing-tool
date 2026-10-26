"""sku_field_edit: dry-run, gates, apply, live, locks, parser, extra edges."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.collection_models import Base, CollectedProduct
from src.db.database_safety import validate_runtime_database
from src.services import sku_field_edit as sfe
from src.utils.store_profile import StoreProfile
from src.utils.text_deletion_guard import deletion_only_text
from scripts.sku_field_edit import build_parser

@pytest.fixture(autouse=True)
def _ebay_production_env(monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "sku_field_edit"
ROWS = json.loads((FIXTURE_DIR / "rows_20261024_preedit.json").read_text(encoding="utf-8"))
TREE = {
    str(cid): (str(name), str(parent))
    for cid, (name, parent) in json.loads((FIXTURE_DIR / "tree_index.json").read_text(encoding="utf-8")).items()
}
BLACKLIST = FIXTURE_DIR / "high_fee_blacklist.json"
AQUA_XML = (FIXTURE_DIR / "aqua_getitem.xml").read_text(encoding="utf-8")


def main_profile() -> StoreProfile:
    return StoreProfile()


def aqua_profile() -> StoreProfile:
    return StoreProfile(
        brand_name="AquaRides",
        store_kind="auto",
        listing_channel="trading",
        ebay_site_id="100",
        category_tree_id="100",
    )


REQUIRED_38208 = [
    "Brand",
    "Color",
    "Item Height",
    "Item Length",
    "Item Width",
    "Type",
    "Upholstery Fabric",
]


def required_loader(category_id: str | None):
    cid = str(category_id or "")
    if cid == "38208":
        return list(REQUIRED_38208)
    if cid == "999001":
        return ["Frame Material"]
    if cid == "184155":
        return []
    if cid in {"303", "20487"}:
        return ["Brand"]
    if cid == "missing":
        return None
    return ["Brand"]


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, owner):
        self.owner = owner

    def put(self, url, headers=None, json=None, timeout=None):
        self.owner.puts.append({"url": url, "headers": headers or {}, "json": deepcopy(json)})
        self.owner._on_put(url, json or {})
        return FakeResponse()

    def post(self, url, headers=None, data=None, timeout=None):
        self.owner.posts.append({"url": url, "headers": headers or {}, "data": data})
        return FakeResponse(text="<Ack>Success</Ack>")


class FakeEbay:
    def __init__(self, items: dict[str, dict], *, qty=None, aqua=False):
        self.base_url = "https://api.ebay.com"
        self.oauth = SimpleNamespace(get_valid_token=lambda: "tok")
        self.session = FakeSession(self)
        self.puts: list[dict] = []
        self.posts: list[dict] = []
        self.forbidden: list[str] = []
        self.items = items
        self.aqua = aqua
        self.break_price_after_put = False
        self.revise_calls: list[dict] = []

    def _sku_from_url(self, url: str) -> str:
        return url.rstrip("/").split("/")[-1]

    def _on_put(self, url: str, payload: dict) -> None:
        if "/inventory_item/" in url:
            sku = self._sku_from_url(url)
            item = self.items[sku]
            product = dict((item["inventory"].get("product") or {}))
            incoming = dict(payload.get("product") or {})
            product.update(incoming)
            item["inventory"]["product"] = product
            if "availability" in payload:
                item["inventory"]["availability"] = deepcopy(payload["availability"])
            if self.break_price_after_put:
                item["offer"]["pricingSummary"] = {"price": {"value": "1.23"}}
        elif "/offer/" in url:
            offer_id = self._sku_from_url(url)
            for item in self.items.values():
                if str(item["offer"].get("offerId")) == offer_id:
                    if "listingDescription" in payload:
                        item["offer"]["listingDescription"] = payload["listingDescription"]
                    if "categoryId" in payload:
                        item["offer"]["categoryId"] = payload["categoryId"]
                    if self.break_price_after_put:
                        item["offer"]["pricingSummary"] = {"price": {"value": "1.23"}}

    def get_inventory_item(self, sku):
        return deepcopy(self.items[sku]["inventory"])

    def get_offers_by_sku(self, sku):
        return [deepcopy(self.items[sku]["offer"])]

    def get_offer(self, offer_id):
        for item in self.items.values():
            if str(item["offer"].get("offerId")) == str(offer_id):
                return deepcopy(item["offer"])
        return {}

    def get_item_description_motors(self, item_id):
        return self.items[item_id]["description"]

    def get_item_compatibility_motors(self, item_id):
        return deepcopy(self.items[item_id]["compatibility"])

    def revise_fixed_price_item_motors(self, item_id, **kwargs):
        from src.services.motors_trading import build_revise_fixed_price_item_xml

        self.revise_calls.append({"item_id": item_id, **deepcopy(kwargs)})
        xml = build_revise_fixed_price_item_xml(
            item_id=item_id,
            description=kwargs.get("description"),
            title=kwargs.get("title"),
            item_specifics=kwargs.get("item_specifics"),
            compatibility=self.items[item_id]["compatibility"],
            replace_all_compatibility=True,
        )
        self.session.post(
            "https://api.ebay.com/ws/api.dll",
            headers={
                "X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem",
                "X-EBAY-API-SITEID": "100",
            },
            data=xml.encode("utf-8"),
        )
        if "description" in kwargs:
            self.items[item_id]["description"] = kwargs["description"]
        if "title" in kwargs:
            self.items[item_id]["title"] = kwargs["title"]
        if "item_specifics" in kwargs:
            self.items[item_id]["aspects"] = deepcopy(kwargs["item_specifics"])
        return {"itemId": item_id, "ack": "Success"}

    def update_offer_category(self, *args, **kwargs):
        self.forbidden.append("update_offer_category")
        raise AssertionError("update_offer_category must not be called")

    def create_or_replace_inventory_item(self, *args, **kwargs):
        self.forbidden.append("create_or_replace_inventory_item")
        raise AssertionError("create_or_replace_inventory_item must not be called")

    def publish_offer(self, *args, **kwargs):
        self.forbidden.append("publish_offer")
        raise AssertionError("publish_offer must not be called")


class FakeTrading:
    def __init__(self, xml: str, ebay: FakeEbay | None = None):
        self.xml = xml
        self.ebay = ebay
        self.revise_item_calls = []

    def get_item(self, item_id, site_id=None):
        assert str(site_id) == "100"
        if self.ebay and str(item_id) in self.ebay.items:
            item = self.ebay.items[str(item_id)]
            aspects_xml = "".join(
                f"<NameValueList><Name>{name}</Name><Value>{value}</Value></NameValueList>"
                for name, values in (item.get("aspects") or {}).items()
                for value in values
            )
            return (
                "<GetItemResponse><Item>"
                f"<ItemID>{item_id}</ItemID>"
                f"<Title>{item.get('title', '')}</Title>"
                f"<Description><![CDATA[{item.get('description', '')}]]></Description>"
                f"<StartPrice>{item.get('start_price', '29.99')}</StartPrice>"
                f"<Quantity>{item.get('quantity', '4')}</Quantity>"
                f"<CategoryID>33651</CategoryID>"
                f"<ItemSpecifics>{aspects_xml}</ItemSpecifics>"
                "</Item></GetItemResponse>"
            )
        return self.xml

    def revise_item(self, *args, **kwargs):
        self.revise_item_calls.append((args, kwargs))
        raise AssertionError("EbayTradingClient.revise_item must not be called")


def inventory_item(row: dict, *, qty=None) -> dict:
    opt = row["optimization"]
    quantity = row["stock"] if qty is None else qty
    return {
        "sku": row["sku"],
        "inventory": {
            "product": {
                "title": opt["title"],
                "description": opt["description"],
                "aspects": deepcopy(opt["aspects"]),
            },
            "availability": {"shipToLocationAvailability": {"quantity": quantity}},
            "condition": "NEW",
        },
        "offer": {
            "offerId": f"off-{row['sku']}",
            "listingId": row["listing_id"],
            "listingDescription": opt["description"],
            "categoryId": opt["categoryId"],
            "availableQuantity": quantity,
            "pricingSummary": {"price": {"value": str(row["price"])}},
        },
    }


def seed_db(path: Path, rows=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        for raw in rows or ROWS:
            session.add(
                CollectedProduct(
                    sku=raw["sku"],
                    title=raw["title"],
                    price=raw["price"],
                    stock=raw["stock"],
                    status=raw["status"],
                    listing_id=raw.get("listing_id"),
                    shipping=raw.get("shipping") or 0,
                    url=raw.get("url"),
                    images=raw.get("images") or [],
                    videos=raw.get("videos") or [],
                    description=raw.get("description") or "",
                    attributes=raw.get("attributes") or {},
                    specs=raw.get("specs") or {},
                    optimization=raw["optimization"],
                    logs=raw.get("logs") or [],
                )
            )
        session.commit()
    engine.dispose()
    return path


def make_ctx(tmp_path: Path, *, store="main", profile=None, ebay=None, trading=None, db_rows=None):
    db_path = seed_db(tmp_path / "ebay_collection.db", db_rows)
    return sfe.SkuFieldEditContext(
        db_path=db_path,
        store=store,
        profile=profile or main_profile(),
        sleep=lambda _s: None,
        backup_root=tmp_path / "backups",
        ebay_client=ebay,
        trading_client=trading,
        category_index=TREE,
        category_indexes={"0": TREE, "100": TREE},
        required_aspect_loader=required_loader,
        high_fee_blacklist_path=BLACKLIST,
        lock_dir=tmp_path / "locks",
        task_lock_dir=tmp_path / "locks",
        maintenance_path=tmp_path / "no-maintenance.lock",
        writer_timeouts={name: 3600 for name in sfe.LISTING_WRITER_TASKS},
        lock_is_stale=lambda info, timeout: not int(info.get("pid") or 0),
    )


def parse(argv: list[str]):
    parser = build_parser()
    return parser.parse_args(argv)


def run(argv: list[str], ctx: sfe.SkuFieldEditContext) -> tuple[int, str, dict]:
    args = parse(argv)
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        code = sfe.run_from_args(args, ctx)
    finally:
        sys.stdout = old
    report = {}
    if args.report and Path(args.report).exists():
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    return code, buf.getvalue(), report


def test_listing_writer_tasks_derived_from_scheduler():
    import scheduler_daemon

    assert sfe.LISTING_WRITER_TASKS <= set(scheduler_daemon.TASK_TIMEOUT)
    assert sfe.WRITER_TASK_TIMEOUT == {
        name: scheduler_daemon.TASK_TIMEOUT[name] for name in sfe.LISTING_WRITER_TASKS
    }
    assert sfe.listing_writer_timeouts() == {
        name: scheduler_daemon.TASK_TIMEOUT[name] for name in sfe.LISTING_WRITER_TASKS
    }


def test_dry_run_default_zero_writes(tmp_path):
    ctx = make_ctx(tmp_path)
    report_path = tmp_path / "report.json"
    manifest = tmp_path / "edits.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "sku": "W714P323654",
                    "ops": [{"op": "strip_claim", "text": "wood", "location": "features"}],
                },
                {
                    "sku": "W465140910",
                    "ops": [{"op": "strip_claim", "text": "concrete", "location": "features"}],
                },
                {
                    "sku": "W465P341531",
                    "ops": [{"op": "strip_claim", "text": "plywood", "location": "features"}],
                },
            ]
        ),
        encoding="utf-8",
    )
    code, out, report = run(
        [
            "--store",
            "main",
            "--skus",
            "W714P323654,W465140910,W465P341531",
            "--manifest",
            str(manifest),
            "--report",
            str(report_path),
        ],
        ctx,
    )
    assert code == 0
    conn = sqlite3.connect(ctx.db_path)
    rows = conn.execute("SELECT sku, status, optimization FROM collected_products").fetchall()
    conn.close()
    assert {row[1] for row in rows} == {"ERROR"}
    assert not (tmp_path / "backups").exists() or not any((tmp_path / "backups").iterdir())
    assert report_path.exists()
    assert report["mode"] == "dry_run"
    by_sku = {item["sku"]: item for item in report["skus"]}
    assert "aspects" in by_sku["W465P341531"]["diff"]
    assert "plywood" not in json.dumps(by_sku["W465P341531"]["diff"]["aspects"]["after"]).casefold()
    assert by_sku["W465140910"]["status"] in {"planned", "already_absent"}
    assert "Frame Material" not in json.dumps(by_sku["W714P323654"].get("diff", {})) or by_sku[
        "W714P323654"
    ]["status"] in {"planned", "refused"}


def test_apply_db_only_whitelist_and_backup(tmp_path):
    ctx = make_ctx(tmp_path)
    report_path = tmp_path / "report.json"
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--note",
            "10/24 plywood scrub",
            "--report",
            str(report_path),
        ],
        ctx,
    )
    assert code == 0
    conn = sqlite3.connect(ctx.db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM collected_products WHERE sku=?", ("W465P341531",)).fetchone())
    conn.close()
    assert row["status"] == "ERROR"
    opt = json.loads(row["optimization"])
    assert all("plywood" not in str(v).casefold() for v in opt["aspects"].get("Features", []))
    assert row["price"] == 74.17
    assert row["stock"] == 123
    backup_dir = Path(report["backup_dir"])
    assert (backup_dir / "rows_before.json").exists()
    db_copy = backup_dir / "ebay_collection.db"
    assert db_copy.exists()
    validate_runtime_database(db_copy)
    assert report["db_backup"]["sha256"]
    assert report["skus"][0]["db_readback"]["ok"] is True


def test_category_303_replay_calls_high_fee_guard(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    calls = []
    real = sfe.guard_category_write_high_fee

    def spy(category_id="", category_name="", **kwargs):
        calls.append(str(category_id))
        return real(category_id, category_name, **kwargs)

    monkeypatch.setattr(sfe, "guard_category_write_high_fee", spy)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--set-category",
            "303",
            "--allow-category",
            "--apply",
            "--note",
            "10/24 category",
            "--report",
            str(tmp_path / "r.json"),
        ],
        ctx,
    )
    assert code == 0
    assert "303" in calls
    conn = sqlite3.connect(ctx.db_path)
    opt = json.loads(conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("W465140910",)).fetchone()[0])
    conn.close()
    assert opt["categoryId"] == "303"
    assert opt["categoryName"] == "Other Hand Tools"


def test_qty_price_not_editable_and_qty_zero_preserved(tmp_path):
    ctx = make_ctx(tmp_path)
    stock_file = tmp_path / "stock.txt"
    stock_file.write_text("0", encoding="utf-8")
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--set-from-file",
            f"stock={stock_file}",
            "--report",
            str(tmp_path / "r.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "qty_price_not_editable" in (out + json.dumps(report))

    qty0 = inventory_item(ROWS[2], qty=0)
    ebay = FakeEbay({"W465P341531": qty0})
    ctx = make_ctx(tmp_path / "live0", ebay=ebay)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--push-live",
            "--note",
            "qty0 content",
            "--report",
            str(tmp_path / "live0.json"),
        ],
        ctx,
    )
    assert code == 0
    assert report["skus"][0]["qty_zero_preserved"] is True
    inv_puts = [item for item in ebay.puts if "/inventory_item/" in item["url"]]
    assert inv_puts
    qty = inv_puts[0]["json"]["availability"]["shipToLocationAvailability"]["quantity"]
    assert qty == 0


def test_category_gates(tmp_path):
    ctx = make_ctx(tmp_path)
    code, out, report = run(
        ["--store", "main", "--sku", "W465140910", "--set-category", "303", "--report", str(tmp_path / "a.json")],
        ctx,
    )
    assert code == 2
    assert "category_flag_required" in (out + json.dumps(report))

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--set-category",
            "999882",
            "--allow-category",
            "--report",
            str(tmp_path / "b.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "BLOCKED_HIGH_FEE" in (out + json.dumps(report))

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--set-category",
            "999881",
            "--allow-category",
            "--report",
            str(tmp_path / "c.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "BLOCKED_HIGH_FEE" in (out + json.dumps(report))

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--set-category",
            "900008",
            "--allow-category",
            "--report",
            str(tmp_path / "d.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "not_leaf" in (out + json.dumps(report))

    locked = deepcopy(ROWS[1])
    locked["optimization"] = dict(locked["optimization"])
    locked["optimization"]["categoryApproved"] = True
    locked["optimization"]["categoryApprovedId"] = "20487"
    ctx = make_ctx(tmp_path / "lock", db_rows=[locked])
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--set-category",
            "303",
            "--allow-category",
            "--report",
            str(tmp_path / "e.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "category_locked" in (out + json.dumps(report))


@pytest.mark.parametrize(
    "before,after,ok",
    [
        ("330 lbs capacity", "380 lbs capacity", False),
        ("Fully assembled", "Fully assembled in minutes", False),
        ("Wood frame, not waterproof", "Waterproof, not wood frame", False),
        ("Water&nbsp;resistant", "Water resistant", True),
        ("2-Piece Set", "2 Pack Set", False),
        ('24" rod', "24 in rod", False),
        ("<li>A</li><li>B</li>", "<li>A</li><li>C</li>", False),
    ],
)
def test_text_table_set_from_file(tmp_path, before, after, ok):
    row = deepcopy(ROWS[2])
    row["optimization"]["description"] = before
    ctx = make_ctx(tmp_path, db_rows=[row])
    path = tmp_path / "new.html"
    path.write_text(after, encoding="utf-8")
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--set-from-file",
            f"description={path}",
            "--report",
            str(tmp_path / "t.json"),
        ],
        ctx,
    )
    if ok:
        assert code == 0
        assert report["skus"][0]["status"] == "planned"
    else:
        assert code == 2
        assert "text_add_refused" in (out + json.dumps(report))


def test_text_table_title_and_strips(tmp_path):
    ctx = make_ctx(tmp_path)
    title_path = tmp_path / "title.txt"
    title_path.write_text("Heavy Duty Steel Hand Truck", encoding="utf-8")
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--set-from-file",
            f"title={title_path}",
            "--report",
            str(tmp_path / "title.json"),
        ],
        ctx,
    )
    assert code == 0
    assert report["skus"][0]["status"] == "planned"

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--report",
            str(tmp_path / "ply.json"),
        ],
        ctx,
    )
    assert code == 0
    after_features = report["skus"][0]["diff"]["aspects"]["after"].get("Features", [])
    assert all("plywood" not in str(item).casefold() for item in after_features)
    assert any("steel" in str(item).casefold() for item in after_features)

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "all",
            "--report",
            str(tmp_path / "hard.json"),
        ],
        ctx,
    )
    # Hardwood-look in description must not match plywood; features still strip.
    desc_after = report["skus"][0]["diff"].get("description", {}).get("after") or ROWS[2]["optimization"]["description"]
    assert "hardwood" in desc_after.casefold()

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--strip-claim",
            "concrete",
            "--location",
            "all",
            "--report",
            str(tmp_path / "conc.json"),
        ],
        ctx,
    )
    desc_after = (report["skus"][0].get("diff") or {}).get("description", {}).get("after") or ROWS[1]["optimization"][
        "description"
    ]
    assert "concretely" in desc_after.casefold()
    if report["skus"][0]["status"] == "planned":
        feats = report["skus"][0]["diff"]["aspects"]["after"].get("Features", [])
        assert all("concrete" not in str(item).casefold() or "concretely" in str(item).casefold() for item in feats)

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W714P323654",
            "--remove-aspect-value",
            "Frame Material=Wood",
            "--report",
            str(tmp_path / "wood.json"),
        ],
        ctx,
    )
    assert code == 0
    sku = report["skus"][0]
    assert sku["status"] == "planned"
    assert sku["diff"]["aspects"]["removed"]["Frame Material"] == ["Wood"]
    desc = ROWS[0]["optimization"]["description"]
    assert "Solid wood legs" in desc

    synth = deepcopy(ROWS[0])
    synth["optimization"]["categoryId"] = "999001"
    ctx = make_ctx(tmp_path / "synth", db_rows=[synth])
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W714P323654",
            "--remove-aspect-value",
            "Frame Material=Wood",
            "--report",
            str(tmp_path / "synth.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "required_aspect_would_empty" in (out + json.dumps(report))


def test_required_aspect_fail_closed(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.required_aspect_loader = lambda _cid: None
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W714P323654",
            "--remove-aspect",
            "Brand",
            "--report",
            str(tmp_path / "r.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "required_aspect_would_empty" in (out + json.dumps(report))


def test_drift_exit_5(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    real = sfe.load_row
    state = {"n": 0}

    def wrapped(conn, sku):
        row = real(conn, sku)
        state["n"] += 1
        if state["n"] >= 2 and row:
            row = dict(row)
            row["stock"] = 1
            return row
        return row

    monkeypatch.setattr(sfe, "load_row", wrapped)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--note",
            "drift",
            "--report",
            str(tmp_path / "d.json"),
        ],
        ctx,
    )
    assert code == 5
    conn = sqlite3.connect(ctx.db_path)
    opt = json.loads(conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("W465P341531",)).fetchone()[0])
    conn.close()
    assert any("plywood" in str(v).casefold() for v in opt["aspects"]["Features"])


def test_readback_mismatch_restore_and_restore_failed(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    real_ok = sfe.db_readback_ok

    def mismatch(conn, plan):
        return False, ["title"]

    monkeypatch.setattr(sfe, "db_readback_ok", mismatch)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--note",
            "tamper",
            "--report",
            str(tmp_path / "t.json"),
        ],
        ctx,
    )
    assert code == 3
    conn = sqlite3.connect(ctx.db_path)
    opt = json.loads(conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("W465P341531",)).fetchone()[0])
    conn.close()
    assert any("plywood" in str(v).casefold() for v in opt["aspects"]["Features"])

    monkeypatch.setattr(sfe, "restore_db_row", lambda *a, **k: False)
    ctx = make_ctx(tmp_path / "fail")
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--note",
            "tamper2",
            "--report",
            str(tmp_path / "t2.json"),
        ],
        ctx,
    )
    assert code == 4
    assert "RESTORE_FAILED" in out


def test_live_main_description_payload_and_restore(tmp_path):
    row = ROWS[2]
    ebay = FakeEbay({row["sku"]: inventory_item(row)})
    ctx = make_ctx(tmp_path, ebay=ebay)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--apply",
            "--push-live",
            "--note",
            "live desc",
            "--report",
            str(tmp_path / "l.json"),
        ],
        ctx,
    )
    assert code == 0
    offer_puts = [item for item in ebay.puts if "/offer/" in item["url"]]
    assert offer_puts
    live = ebay.get_offer("off-W465P341531")
    payload = offer_puts[0]["json"]
    from src.utils.claim_strip import OFFER_GET_ONLY_KEYS, offer_description_only_put_payload

    expected = offer_description_only_put_payload(live, payload["listingDescription"])
    # Compare against the GET-shaped live *before* put mutated it: rebuild from recorded.
    before = inventory_item(row)["offer"]
    rebuilt = offer_description_only_put_payload(before, payload["listingDescription"])
    before_put = {k: v for k, v in before.items() if k not in OFFER_GET_ONLY_KEYS}
    after_put = dict(rebuilt)
    changed = {k for k in set(before_put) | set(after_put) if before_put.get(k) != after_put.get(k)}
    assert changed == {"listingDescription"}
    assert not ebay.forbidden

    ebay2 = FakeEbay({row["sku"]: inventory_item(row)})
    ebay2.break_price_after_put = True
    ctx = make_ctx(tmp_path / "restore", ebay=ebay2)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--apply",
            "--push-live",
            "--note",
            "price drift",
            "--report",
            str(tmp_path / "p.json"),
        ],
        ctx,
    )
    assert code == 3
    conn = sqlite3.connect(ctx.db_path)
    opt = json.loads(conn.execute("SELECT optimization FROM collected_products WHERE sku=?", (row["sku"],)).fetchone()[0])
    conn.close()
    assert "plywood" in json.dumps(opt).casefold()


def test_live_category_never_calls_update_offer_category(tmp_path):
    row = deepcopy(ROWS[1])
    ebay = FakeEbay({row["sku"]: inventory_item(row)})
    ctx = make_ctx(tmp_path, ebay=ebay)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--set-category",
            "303",
            "--allow-category",
            "--apply",
            "--push-live",
            "--note",
            "cat live",
            "--report",
            str(tmp_path / "c.json"),
        ],
        ctx,
    )
    assert code == 0
    assert "update_offer_category" not in ebay.forbidden
    offer_puts = [item for item in ebay.puts if "/offer/" in item["url"]]
    assert offer_puts
    assert offer_puts[0]["json"]["categoryId"] == "303"


def test_live_aqua_revise_and_category_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", aqua_profile)
    row = deepcopy(ROWS[2])
    row["listing_id"] = "366333333333"
    parsed = sfe.parse_getitem_xml(AQUA_XML)
    ebay = FakeEbay(
        {
            "366333333333": {
                "description": parsed["description"],
                "title": parsed["title"],
                "aspects": parsed["aspects"],
                "start_price": parsed["start_price"] or "29.99",
                "quantity": parsed["quantity"] or "4",
                "compatibility": [
                    {"compatibilityProperties": [
                        {"name": "Year", "value": "2018"},
                        {"name": "Make", "value": "Ford"},
                        {"name": "Model", "value": "F-150"},
                    ]},
                    {"compatibilityProperties": [
                        {"name": "Year", "value": "2019"},
                        {"name": "Make", "value": "Ford"},
                        {"name": "Model", "value": "F-150"},
                    ]},
                ],
            }
        },
        aqua=True,
    )
    trading = FakeTrading(AQUA_XML, ebay=ebay)
    ctx = make_ctx(tmp_path, store="aqua", profile=aqua_profile(), ebay=ebay, trading=trading, db_rows=[row])
    # fetch_live looks up by listing_id for aqua description via ebay methods keyed by item_id
    code, out, report = run(
        [
            "--store",
            "aqua",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--push-live",
            "--note",
            "aqua strip",
            "--report",
            str(tmp_path / "a.json"),
        ],
        ctx,
    )
    assert code == 0
    assert ebay.revise_calls
    specifics = ebay.revise_calls[0]["item_specifics"]
    assert "Plywood backing plate" not in json.dumps(specifics)
    assert specifics["Brand"] == ["AquaRides"]
    assert any(item["headers"].get("X-EBAY-API-SITEID") == "100" for item in ebay.posts)
    assert any("ReplaceAll>true" in str(item.get("data") or b"") for item in ebay.posts)
    assert not trading.revise_item_calls

    ctx = make_ctx(tmp_path / "cat", store="aqua", profile=aqua_profile(), ebay=ebay, trading=trading, db_rows=[row])
    code, out, report = run(
        [
            "--store",
            "aqua",
            "--sku",
            "W465P341531",
            "--set-category",
            "303",
            "--allow-category",
            "--push-live",
            "--report",
            str(tmp_path / "ac.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "aqua_live_category_unsupported" in (out + json.dumps(report))


def test_preflight_refusals(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, store="main", profile=aqua_profile())
    code, out, report = run(
        ["--store", "main", "--sku", "W465P341531", "--strip-claim", "x", "--location", "title", "--report", str(tmp_path / "m.json")],
        ctx,
    )
    assert code == 2
    assert "store_profile_mismatch" in (out + json.dumps(report))

    monkeypatch.setenv("EBAY_ENVIRONMENT", "SANDBOX")
    ebay = FakeEbay({ROWS[2]["sku"]: inventory_item(ROWS[2])})
    ebay.base_url = "https://api.sandbox.ebay.com"
    ctx = make_ctx(tmp_path / "sb", ebay=ebay)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--push-live",
            "--report",
            str(tmp_path / "sb.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "push_live_not_production" in (out + json.dumps(report))

    ctx = make_ctx(tmp_path / "maint")
    ctx.maintenance_path.write_text("lock", encoding="utf-8")
    code, out, report = run(
        ["--store", "main", "--sku", "W465P341531", "--strip-claim", "x", "--location", "title", "--report", str(tmp_path / "ml.json")],
        ctx,
    )
    assert code == 2

    ctx = make_ctx(tmp_path / "hl")
    linked = tmp_path / "hl" / "linked.db"
    os.link(ctx.db_path, linked)
    ctx.db_path = linked
    code, out, report = run(
        ["--store", "main", "--sku", "W465P341531", "--strip-claim", "x", "--location", "title", "--report", str(tmp_path / "hl.json")],
        ctx,
    )
    assert code == 2


def test_locks(tmp_path):
    ctx = make_ctx(tmp_path)
    lock = ctx.lock_dir / "sku_field_edit.lock"
    ctx.lock_dir.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "worker_pid": os.getpid()}), encoding="utf-8")
    ctx.acquire_lock = lambda *a, **k: None
    code, out, report = run(
        ["--store", "main", "--sku", "W465P341531", "--strip-claim", "x", "--location", "title", "--report", str(tmp_path / "l1.json")],
        ctx,
    )
    assert code == 2
    assert "another sku_field_edit is running" in out

    ctx = make_ctx(tmp_path / "ap")
    ctx.lock_dir.mkdir(parents=True, exist_ok=True)
    (ctx.lock_dir / "auto_publish.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--note",
            "blocked",
            "--report",
            str(tmp_path / "l2.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "auto_publish" in (out + json.dumps(report))

    ctx = make_ctx(tmp_path / "stale")
    ctx.lock_dir.mkdir(parents=True, exist_ok=True)
    (ctx.lock_dir / "auto_publish.lock").write_text(json.dumps({"pid": 0}), encoding="utf-8")
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--note",
            "stale ok",
            "--report",
            str(tmp_path / "l3.json"),
        ],
        ctx,
    )
    assert code == 0


def test_parser_isolation_and_refusals(tmp_path):
    before = set(sys.modules)
    spec = importlib.util.spec_from_file_location(
        "sku_field_edit_parser_only",
        ROOT / "scripts" / "sku_field_edit.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build_parser()
    after = set(sys.modules) - before
    heavy = [
        name
        for name in after
        if name.startswith(("src.db", "src.clients", "src.services.ebay_auth"))
        or name in {"requests", "sqlite3"}
        or "oauth" in name.lower()
    ]
    assert heavy == []

    ctx = make_ctx(tmp_path)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--set-from-file",
            f"images={tmp_path / 'x.txt'}",
            "--report",
            str(tmp_path / "u.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "field_not_editable" in (out + json.dumps(report))

    code, out, report = run(
        [
            "--store",
            "main",
            "--skus",
            ",".join(f"SKU{i}" for i in range(51)),
            "--strip-claim",
            "x",
            "--location",
            "title",
            "--report",
            str(tmp_path / "n.json"),
        ],
        ctx,
    )
    assert code == 2

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--apply",
            "--report",
            str(tmp_path / "note.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "note_required" in (out + json.dumps(report))


def test_extra_edge_protected_zone_mixed_case_empty_ul_and_both_fields(tmp_path):
    row = deepcopy(ROWS[2])
    row["optimization"]["description"] = "<h2>PACKAGE INCLUDES</h2><p>One plywood board</p>"
    row["optimization"]["title"] = "Plywood Steel Hand Truck Extra Long Title"
    ctx = make_ctx(tmp_path, db_rows=[row])
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--report",
            str(tmp_path / "prot.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "protected_zone" in (out + json.dumps(report))

    row2 = deepcopy(ROWS[2])
    row2["optimization"]["description"] = "<ul><li>Plywood deck resists warping</li></ul>"
    row2["optimization"]["title"] = "Plywood Steel Hand Truck Extra Long Title"
    ctx = make_ctx(tmp_path / "both", db_rows=[row2])
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "Plywood",
            "--location",
            "all",
            "--report",
            str(tmp_path / "both.json"),
        ],
        ctx,
    )
    assert code == 0
    sku = report["skus"][0]
    assert sku["status"] == "planned"
    desc = sku["diff"].get("description", {}).get("after", "")
    assert "plywood" not in desc.casefold()
    assert "<li>" not in desc
    assert "plywood" not in sku["diff"].get("title", {}).get("after", "").casefold()


def test_deletion_only_helper_reorders_and_units():
    ok, offenders = deletion_only_text("Wood frame, not waterproof", "Waterproof, not wood frame")
    assert ok is False
    ok, _ = deletion_only_text("Water&nbsp;resistant", "Water resistant")
    assert ok is True


def test_push_live_constructs_real_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    created = {}

    class FakeReal:
        base_url = "https://api.ebay.com"

    def fake_factory(environment="PRODUCTION"):
        created["env"] = environment
        created["real"] = True
        return FakeReal()

    def fake_trading(ebay):
        created["trading"] = ebay
        return SimpleNamespace()

    monkeypatch.setattr("src.clients.real_ebay_client.create_real_ebay_client", fake_factory)
    monkeypatch.setattr("src.clients.ebay_trading_client.EbayTradingClient", fake_trading)
    seed_db(tmp_path / "ebay_collection.db")
    monkeypatch.setattr(sfe, "DATABASE_PATH", tmp_path / "ebay_collection.db")
    monkeypatch.setattr(sfe, "MAINTENANCE_PATH", tmp_path / "no-maintenance.lock")
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        args = parse(
            [
                "--store",
                "main",
                "--sku",
                "W465P341531",
                "--strip-claim",
                "plywood",
                "--location",
                "description",
                "--push-live",
                "--report",
                str(tmp_path / "h1.json"),
            ]
        )
        sfe.run_from_args(args, None)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert created.get("real") is True
    assert "https://api.ebay.com" in out
    assert "EBAY_ENVIRONMENT=PRODUCTION" in out

    created.clear()
    args = parse(
        [
            "--store",
            "aqua",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--push-live",
            "--report",
            str(tmp_path / "h1a.json"),
        ]
    )
    aqua = aqua_profile()
    monkeypatch.setattr(sfe, "get_store_profile", lambda: aqua)
    sys.stdout = io.StringIO()
    try:
        sfe.run_from_args(args, None)
    except Exception:
        pass
    finally:
        sys.stdout = old
    assert created.get("real") is True
    assert "trading" in created


def test_live_strip_keeps_live_only_li_never_pushes_db_only_text(tmp_path):
    row = deepcopy(ROWS[2])
    row["optimization"]["description"] = (
        "<ul><li>Plywood deck resists warping</li><li>DB-only extra sentence</li></ul>"
    )
    item = inventory_item(row)
    live_desc = "<ul><li>Plywood deck resists warping</li><li>Live-only extra pad</li></ul>"
    item["inventory"]["product"]["description"] = live_desc
    item["offer"]["listingDescription"] = live_desc
    ebay = FakeEbay({"W465P341531": item})
    ctx = make_ctx(tmp_path, ebay=ebay)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--apply",
            "--push-live",
            "--note",
            "h2 live vs db",
            "--report",
            str(tmp_path / "h2.json"),
        ],
        ctx,
    )
    assert code == 0
    offer_puts = [item for item in ebay.puts if "/offer/" in item["url"]]
    assert offer_puts
    pushed = offer_puts[0]["json"]["listingDescription"]
    assert "Live-only extra pad" in pushed
    assert "plywood" not in pushed.casefold()
    assert "DB-only extra sentence" not in pushed

    only_claim = deepcopy(ROWS[2])
    only_claim["optimization"]["description"] = (
        "<ul><li>Plywood deck resists warping</li><li>DB-only leftover</li></ul>"
    )
    only_item = inventory_item(only_claim)
    only_live = "<ul><li>Plywood deck resists warping</li></ul>"
    only_item["inventory"]["product"]["description"] = only_live
    only_item["offer"]["listingDescription"] = only_live
    only_ebay = FakeEbay({"W465P341531": only_item})
    only_ctx = make_ctx(tmp_path / "empty-live", ebay=only_ebay, db_rows=[only_claim])
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "description",
            "--apply",
            "--push-live",
            "--note",
            "h2 empty live after",
            "--report",
            str(tmp_path / "h2-empty.json"),
        ],
        only_ctx,
    )
    assert code == 0
    empty_puts = [item for item in only_ebay.puts if "/offer/" in item["url"]]
    assert empty_puts
    empty_pushed = empty_puts[0]["json"]["listingDescription"]
    assert "plywood" not in empty_pushed.casefold()
    assert "DB-only leftover" not in empty_pushed
    assert empty_pushed != only_live


def test_zero_required_aspects_184155(tmp_path):
    ctx = make_ctx(tmp_path)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--remove-aspect",
            "Function",
            "--report",
            str(tmp_path / "m3a.json"),
        ],
        ctx,
    )
    assert code == 0
    assert report["skus"][0]["status"] == "planned"
    assert "Function" in report["skus"][0]["diff"]["aspects"]["removed"]

    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--set-category",
            "184155",
            "--allow-category",
            "--report",
            str(tmp_path / "m3b.json"),
        ],
        ctx,
    )
    assert code == 0
    assert report["skus"][0]["diff"]["categoryId"]["after"] == "184155"


def test_same_category_already_absent(tmp_path):
    ctx = make_ctx(tmp_path)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465140910",
            "--set-category",
            "20487",
            "--allow-category",
            "--report",
            str(tmp_path / "l3.json"),
        ],
        ctx,
    )
    assert code == 0
    assert report["skus"][0]["status"] == "already_absent"


def test_set_from_file_unreadable(tmp_path):
    ctx = make_ctx(tmp_path)
    code, out, report = run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--set-from-file",
            f"description={tmp_path / 'missing.html'}",
            "--report",
            str(tmp_path / "l6.json"),
        ],
        ctx,
    )
    assert code == 2
    assert "file_unreadable" in (out + json.dumps(report))


def test_running_tool_does_not_create_scheduler_log(tmp_path):
    log = ROOT / "logs" / "scheduler_daemon.log"
    existed = log.exists()
    mtime = log.stat().st_mtime if existed else None
    ctx = make_ctx(tmp_path)
    run(
        [
            "--store",
            "main",
            "--sku",
            "W465P341531",
            "--strip-claim",
            "plywood",
            "--location",
            "features",
            "--report",
            str(tmp_path / "m1.json"),
        ],
        ctx,
    )
    source = Path(sfe.__file__).read_text(encoding="utf-8")
    assert "scheduler_daemon" not in source
    if existed:
        assert log.stat().st_mtime == mtime
    else:
        assert not log.exists()
