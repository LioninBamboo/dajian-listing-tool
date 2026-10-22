"""CLI mutual-exclusion and fake-eBay end-to-end tests for claim strip."""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import types
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CSV = ROOT / "tests" / "fixtures" / "claims_manifest_fixture.csv"
BEFORE_JSON = ROOT / "tests" / "fixtures" / "claim_strip" / "main_before_subset.json"


@pytest.fixture(autouse=True, scope="module")
def stub_sys_modules_for_cli():
    requests_stub = types.SimpleNamespace(
        get=lambda *_args, **_kwargs: None,
        post=lambda *_args, **_kwargs: None,
        Session=object,
        utils=types.SimpleNamespace(quote=lambda value: value),
        exceptions=types.SimpleNamespace(HTTPError=Exception),
    )
    stubs = {
        "dotenv": types.SimpleNamespace(load_dotenv=lambda *_args, **_kwargs: None),
        "requests": requests_stub,
    }
    saved = {}
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


def load_audit_module():
    spec = importlib.util.spec_from_file_location(
        "audit_fix_active_listings_claim_strip",
        ROOT / "scripts" / "audit_fix_active_listings.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load_audit_module()


def _parser_error(argv):
    parser = audit.create_parser()
    try:
        args = parser.parse_args(argv)
        audit.validate_fix_scope(args, parser)
    except SystemExit as exc:
        assert exc.code == 2
        return
    raise AssertionError(f"expected parser.error for {argv}")


def test_parser_requires_manifest_and_fix_key_together():
    _parser_error(
        [
            "--source-report",
            "r.json",
            "--issue-type",
            "semantic_feature",
            "--claims-manifest",
            "m.csv",
        ]
    )
    _parser_error(
        [
            "--source-report",
            "r.json",
            "--issue-type",
            "semantic_feature",
            "--fix-key",
            "__strip_claims_from_manifest__",
        ]
    )


def test_parser_rejects_other_fix_keys_with_strip():
    _parser_error(
        [
            "--source-report",
            "r.json",
            "--issue-type",
            "semantic_feature",
            "--claims-manifest",
            "m.csv",
            "--fix-key",
            "__strip_claims_from_manifest__",
            "--fix-key",
            "__semantic_rebuild_from_source__",
        ]
    )
    _parser_error(
        [
            "--source-report",
            "r.json",
            "--issue-type",
            "semantic_feature",
            "--claims-manifest",
            "m.csv",
            "--fix-key",
            "__strip_claims_from_manifest__",
            "--fix-key",
            "__claim_diff_violations__",
        ]
    )
    _parser_error(
        [
            "--source-report",
            "r.json",
            "--issue-type",
            "semantic_feature",
            "--claims-manifest",
            "m.csv",
            "--fix-key",
            "__strip_claims_from_manifest__",
            "--fix-key",
            "__source_parameter_rebuild__",
        ]
    )


def test_parser_allows_source_report_plus_sku_for_strip_only():
    parser = audit.create_parser()
    args = parser.parse_args(
        [
            "--source-report",
            "r.json",
            "--issue-type",
            "semantic_feature",
            "--claims-manifest",
            "m.csv",
            "--fix-key",
            "__strip_claims_from_manifest__",
            "--sku",
            "W465P154347",
            "--claims-location",
            "description",
        ]
    )
    audit.validate_fix_scope(args, parser)
    assert args.claims_location == "description"
    assert args.sku == "W465P154347"


def test_parser_still_rejects_source_report_plus_sku_for_other_keys():
    _parser_error(
        [
            "--source-report",
            "r.json",
            "--issue-type",
            "missing_video",
            "--sku",
            "W1",
        ]
    )


def test_fix_key_matches_semantic_only():
    assert audit._fix_key_matches_issue("__strip_claims_from_manifest__", "semantic_feature")
    assert audit._fix_key_matches_issue("__strip_claims_from_manifest__", "semantic_material")
    assert not audit._fix_key_matches_issue("__strip_claims_from_manifest__", "claim_capacity")
    assert not audit._fix_key_matches_issue("__strip_claims_from_manifest__", "missing_video")


class _Resp:
    def __init__(self, status_code=204, text=""):
        self.status_code = status_code
        self.text = text


class FakeEbay:
    def __init__(self, listings: dict):
        self.listings = listings
        self.oauth = types.SimpleNamespace(get_valid_token=lambda: "token")
        self.base_url = "https://api.ebay.example"
        self.session = types.SimpleNamespace(put=self._put)
        self.puts = []
        self.offer_updates = []
        self.publishes = []
        self.readback_override = None
        self.write_enabled = True

    def _rec(self, sku):
        return self.listings[sku]

    def get_inventory_item(self, sku):
        if self.readback_override and self.puts:
            return deepcopy(self.readback_override[0])
        return deepcopy(self._rec(sku)["inv"])

    def get_offers_by_sku(self, sku):
        if self.readback_override and self.puts:
            return [deepcopy(self.readback_override[1])]
        return [deepcopy(self._rec(sku)["offers"][0])]

    def _put(self, url, headers=None, json=None, timeout=None):
        self.puts.append({"url": url, "json": deepcopy(json), "headers": headers})
        if self.write_enabled and json is not None:
            sku = url.rstrip("/").split("/")[-1]
            if sku in self.listings:
                self.listings[sku]["inv"] = deepcopy(json)
                # Preserve fields the snapshot payload may omit by merging.
                current = self.listings[sku]["inv"]
                if "sku" not in current:
                    current["sku"] = sku
        return _Resp(204)

    def update_offer_category(self, offer_id, category_id, price=None, listing_description=None):
        self.offer_updates.append(
            {
                "offer_id": offer_id,
                "category_id": category_id,
                "price": price,
                "listing_description": listing_description,
            }
        )
        for rec in self.listings.values():
            offer = rec["offers"][0]
            if offer.get("offerId") == offer_id:
                if listing_description is not None:
                    offer["listingDescription"] = listing_description
                offer["categoryId"] = category_id
                if price is not None:
                    offer.setdefault("pricingSummary", {}).setdefault("price", {})["value"] = str(price)
        return True

    def publish_offer(self, offer_id):
        self.publishes.append(offer_id)
        return {"listingId": "published"}


def _listings():
    return json.loads(BEFORE_JSON.read_text(encoding="utf-8"))


def _report_from_csv():
    from src.utils import claim_strip as cs

    rows, _ = cs.load_claims_manifest(FIXTURE_CSV)
    by_sku = {}
    for row in rows:
        if row.recommendation != cs.STRIP_NOW:
            continue
        by_sku.setdefault(row.sku, []).append(
            {
                "type": "semantic_feature",
                "severity": "HIGH",
                "detail": f"unsourced FactSheet claim: {row.claim}",
            }
        )
    return {"issues": [{"sku": sku, "issues": issues} for sku, issues in by_sku.items()]}


def _memory_db(skus):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT, updated_at TEXT)"
    )
    for sku in skus:
        conn.execute(
            "INSERT INTO collected_products (sku, optimization, updated_at) VALUES (?, ?, ?)",
            (sku, json.dumps({"title": sku, "description": "OLD", "aspects": {"Keep": ["Yes"]}}), "2026-01-01"),
        )
    conn.commit()
    return conn


def _args(tmp_path, *, fix=False, sku="W46538540"):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report_from_csv()), encoding="utf-8")
    parser = audit.create_parser()
    argv = [
        "--source-report",
        str(report),
        "--issue-type",
        "semantic_feature",
        "--claims-manifest",
        str(FIXTURE_CSV),
        "--fix-key",
        "__strip_claims_from_manifest__",
        "--sku",
        sku,
    ]
    if fix:
        argv.append("--fix")
    args = parser.parse_args(argv)
    audit.validate_fix_scope(args, parser)
    return args, parser


def test_dry_run_makes_no_ebay_or_sqlite_writes(tmp_path, monkeypatch):
    args, parser = _args(tmp_path, fix=False)
    listings = _listings()
    client = FakeEbay(listings)
    conn = _memory_db(["W46538540"])
    persist_calls = []
    monkeypatch.setattr(audit, "persist_listing_audit_state", lambda *a, **k: persist_calls.append((a, k)) or True)
    before_opt = conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("W46538540",)).fetchone()[0]
    buf = io.StringIO()
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=conn,
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_test",
        required_aspect_loader=lambda _cid: [],
        stdout=buf,
    )
    assert rc == 0
    assert client.puts == []
    assert client.offer_updates == []
    assert persist_calls == []
    after_opt = conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("W46538540",)).fetchone()[0]
    assert after_opt == before_opt
    results = json.loads((tmp_path / "claim_strip" / "dryrun_20261022_test.json").read_text())
    assert results["mode"] == "dryrun"
    assert any(entry["status"] == "will_strip" for entry in results["entries"])


def test_live_put_keeps_before_image_except_description_and_aspects(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W2787P378160")
    listings = _listings()
    before_inv = deepcopy(listings["W2787P378160"]["inv"])
    before_offer = deepcopy(listings["W2787P378160"]["offers"][0])
    client = FakeEbay(listings)
    conn = _memory_db(["W2787P378160"])
    persist_calls = []
    monkeypatch.setattr(audit, "persist_listing_audit_state", lambda *a, **k: persist_calls.append(1) or True)
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=conn,
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_live",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert persist_calls == []
    assert client.puts, "expected inventory snapshot PUT"
    payload = client.puts[0]["json"]
    assert payload["product"]["title"] == before_inv["product"]["title"]
    assert payload["product"]["imageUrls"] == before_inv["product"]["imageUrls"]
    assert payload.get("availability") == before_inv.get("availability")
    assert payload.get("condition") == before_inv.get("condition")
    assert payload.get("packageWeightAndSize") == before_inv.get("packageWeightAndSize")
    assert payload["product"]["aspects"] != before_inv["product"]["aspects"]
    assert payload["product"]["aspects"]["Type"] == before_inv["product"]["aspects"]["Type"]
    assert payload["product"]["aspects"]["Finish"] == before_inv["product"]["aspects"]["Finish"]
    assert client.offer_updates == [] or all(
        item["category_id"] == before_offer["categoryId"]
        and str(item["price"]) == str(float(before_offer["pricingSummary"]["price"]["value"]))
        for item in client.offer_updates
    )
    opt = json.loads(
        conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("W2787P378160",)).fetchone()[0]
    )
    assert opt["aspects"]["Features"] == payload["product"]["aspects"]["Features"]
    assert opt["title"] == "W2787P378160"
    assert opt["description"] == "OLD"
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_live.json").read_text())
    assert results["mode"] == "live"
    before_files = list((tmp_path / "claim_strip" / "before").glob("W2787P378160_*.json"))
    assert before_files


def test_live_offer_update_keeps_category_and_price(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    before_offer = deepcopy(listings["W46538540"]["offers"][0])
    client = FakeEbay(listings)
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_offer",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert client.offer_updates
    update = client.offer_updates[0]
    assert update["category_id"] == before_offer["categoryId"]
    assert float(update["price"]) == float(before_offer["pricingSummary"]["price"]["value"])
    assert "Industrial-Grade Finish" not in (update["listing_description"] or "")
    assert client.publishes == [before_offer["offerId"]]


def test_readback_mismatch_restores_before_image(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    before_inv = deepcopy(listings["W46538540"]["inv"])
    before_offer = deepcopy(listings["W46538540"]["offers"][0])
    client = FakeEbay(listings)
    drifted_offer = deepcopy(before_offer)
    drifted_offer["listingDescription"] = before_offer["listingDescription"] + "<!--drift-->"
    client.readback_override = (before_inv, drifted_offer)
    buf = io.StringIO()
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_restore",
        required_aspect_loader=lambda _cid: [],
        stdout=buf,
    )
    assert rc == 0
    assert "FAILED_RESTORED" in buf.getvalue()
    # Last inventory PUT must be the original before-image snapshot.
    restore_payload = client.puts[-1]["json"]
    assert restore_payload["product"]["title"] == before_inv["product"]["title"]
    assert restore_payload["product"]["aspects"] == before_inv["product"]["aspects"]
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_restore.json").read_text())
    assert any(entry["status"] == "failed_restored" for entry in results["entries"])


def test_rerun_after_success_makes_no_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    client = FakeEbay(listings)
    conn = _memory_db(["W46538540"])
    audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=conn,
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_first",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    puts_after_first = len(client.puts)
    offers_after_first = len(client.offer_updates)
    assert puts_after_first >= 1
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=conn,
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_second",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert len(client.puts) == puts_after_first
    assert len(client.offer_updates) == offers_after_first
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_second.json").read_text())
    assert all(entry["status"] == "already_absent" for entry in results["entries"])


def test_fix_refuses_non_production(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "SANDBOX")
    args, parser = _args(tmp_path, fix=True)
    buf = io.StringIO()
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=FakeEbay(_listings()),
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_sbx",
        required_aspect_loader=lambda _cid: [],
        stdout=buf,
    )
    assert rc == 2
    assert "REFUSE --fix" in buf.getvalue()
