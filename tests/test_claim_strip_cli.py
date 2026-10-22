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
        self.inventory_puts = []
        self.offer_puts = []
        self.offer_updates = []
        self.publishes = []
        self.write_enabled = True
        self.after_first_pair = None
        self.inject_readback = None
        self.drift_after_first_write = False
        self._drifted = False
        self._offer_pairs = 0
        self.offer_put_error = None
        self.offer_put_fail_remaining = 0
        self.publish_fail_remaining = 0
        self.inventory_put_fail_on = set()
        self._inventory_put_count = 0
        self.missing_skus = set()
        self.fetch_errors = {}

    def _rec(self, sku):
        return self.listings[sku]

    def _maybe_inject(self):
        if self.inject_readback:
            inv, offer = self.inject_readback
            self.inject_readback = None
            return deepcopy(inv), deepcopy(offer)
        return None

    def get_inventory_item(self, sku):
        if sku in self.fetch_errors:
            raise RuntimeError(self.fetch_errors[sku])
        if sku in self.missing_skus:
            return {}
        injected = self._peek_inject_inv()
        if injected is not None:
            return injected
        return deepcopy(self._rec(sku)["inv"])

    def _peek_inject_inv(self):
        if self.inject_readback:
            return deepcopy(self.inject_readback[0])
        return None

    def get_offers_by_sku(self, sku):
        if sku in self.fetch_errors:
            raise RuntimeError(self.fetch_errors[sku])
        if sku in self.missing_skus:
            return []
        if self.inject_readback:
            offer = deepcopy(self.inject_readback[1])
            self.inject_readback = None
            return [offer]
        offers = [deepcopy(self._rec(sku)["offers"][0])]
        self._offer_pairs += 1
        if self._offer_pairs == 1 and self.after_first_pair:
            self.after_first_pair(self.listings)
        return offers

    def get_offer(self, offer_id):
        for rec in self.listings.values():
            offer = rec["offers"][0]
            if offer.get("offerId") == offer_id:
                return deepcopy(offer)
        return None

    def _put(self, url, headers=None, json=None, timeout=None):
        self.puts.append({"url": url, "json": deepcopy(json), "headers": headers})
        if "/offer/" in url:
            if self.offer_put_fail_remaining > 0:
                self.offer_put_fail_remaining -= 1
                raise self.offer_put_error or RuntimeError("offer PUT failed")
            self.offer_puts.append({"url": url, "json": deepcopy(json)})
            offer_id = url.rstrip("/").split("/")[-1]
            if self.write_enabled and json is not None:
                for rec in self.listings.values():
                    offer = rec["offers"][0]
                    if offer.get("offerId") != offer_id:
                        continue
                    kept = {key: offer.get(key) for key in ("offerId", "listing", "status")}
                    offer.clear()
                    offer.update(deepcopy(json))
                    for key, value in kept.items():
                        if value is not None and key not in offer:
                            offer[key] = value
            self._maybe_queue_drift()
            return _Resp(204)

        self._inventory_put_count += 1
        self.inventory_puts.append({"url": url, "json": deepcopy(json)})
        if self._inventory_put_count in self.inventory_put_fail_on:
            return _Resp(500, "inventory PUT failed")
        if self.write_enabled and json is not None:
            sku = url.rstrip("/").split("/")[-1]
            if sku in self.listings:
                merged = deepcopy(self.listings[sku]["inv"])
                merged.update(deepcopy(json))
                if "sku" not in merged:
                    merged["sku"] = sku
                self.listings[sku]["inv"] = merged
        self._maybe_queue_drift()
        return _Resp(204)

    def _maybe_queue_drift(self):
        if self.drift_after_first_write and not self._drifted and self.inventory_puts:
            sku = self.inventory_puts[0]["url"].rstrip("/").split("/")[-1]
            drifted_offer = deepcopy(self._rec(sku)["offers"][0])
            drifted_offer["listingDescription"] = (
                str(drifted_offer.get("listingDescription") or "") + "<!--drift-->"
            )
            self.inject_readback = (deepcopy(self._rec(sku)["inv"]), drifted_offer)
            self._drifted = True

    def update_offer_category(self, offer_id, category_id, price=None, listing_description=None):
        raise AssertionError("claim strip must not call update_offer_category")

    def publish_offer(self, offer_id):
        self.publishes.append(offer_id)
        if self.publish_fail_remaining > 0:
            self.publish_fail_remaining -= 1
            raise RuntimeError("publish_offer failed")
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
    assert client.offer_puts == []
    assert client.publishes == [before_offer["offerId"]]
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
    assert client.offer_puts
    payload = client.offer_puts[0]["json"]
    assert payload["categoryId"] == before_offer["categoryId"]
    assert payload["availableQuantity"] == before_offer["availableQuantity"] == 2
    assert payload["pricingSummary"] == before_offer["pricingSummary"]
    assert "Industrial-Grade Finish" not in (payload["listingDescription"] or "")
    assert client.publishes == [before_offer["offerId"]]
    assert client.offer_updates == []


def test_readback_mismatch_restores_before_image(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    before_inv = deepcopy(listings["W46538540"]["inv"])
    before_offer = deepcopy(listings["W46538540"]["offers"][0])
    client = FakeEbay(listings)
    client.drift_after_first_write = True
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
    assert "failed_restored" not in buf.getvalue()
    restore_payload = client.inventory_puts[-1]["json"]
    assert restore_payload["product"]["title"] == before_inv["product"]["title"]
    assert restore_payload["product"]["aspects"] == before_inv["product"]["aspects"]
    assert listings["W46538540"]["offers"][0]["listingDescription"] == before_offer["listingDescription"]
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_restore.json").read_text())
    assert any(entry.get("apply_status") == "write_failed" for entry in results["entries"])
    assert all(entry["status"] != "failed_restored" for entry in results["entries"])
    assert all(entry["status"] != "restore_failed" for entry in results["entries"])


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
    offers_after_first = len(client.offer_puts)
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
    assert len(client.offer_puts) == offers_after_first
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


def _args_sku_file(tmp_path, skus, *, fix=True):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report_from_csv()), encoding="utf-8")
    sku_file = tmp_path / "skus.txt"
    sku_file.write_text("\n".join(skus) + "\n", encoding="utf-8")
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
        "--sku-file",
        str(sku_file),
    ]
    if fix:
        argv.append("--fix")
    args = parser.parse_args(argv)
    audit.validate_fix_scope(args, parser)
    return args, parser


def test_live_qty_of_two_stays_two(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    listings["W46538540"]["inv"]["availability"]["shipToLocationAvailability"]["quantity"] = 2
    listings["W46538540"]["offers"][0]["availableQuantity"] = 2
    client = FakeEbay(listings)
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_qty2",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    inv_payload = client.inventory_puts[0]["json"]
    assert inv_payload["availability"]["shipToLocationAvailability"]["quantity"] == 2
    offer_payload = client.offer_puts[0]["json"]
    assert offer_payload["availableQuantity"] == 2
    assert listings["W46538540"]["inv"]["availability"]["shipToLocationAvailability"]["quantity"] == 2
    assert listings["W46538540"]["offers"][0]["availableQuantity"] == 2


def test_supplier_oos_on_offer_put_restores_inventory(tmp_path, monkeypatch):
    from src.utils.ebay_quantity import SupplierOutOfStockError

    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    before_inv = deepcopy(listings["W46538540"]["inv"])
    before_offer = deepcopy(listings["W46538540"]["offers"][0])
    client = FakeEbay(listings)
    client.offer_put_error = SupplierOutOfStockError("supplier OOS")
    client.offer_put_fail_remaining = 1
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_oos",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert listings["W46538540"]["inv"]["product"]["aspects"] == before_inv["product"]["aspects"]
    assert listings["W46538540"]["inv"]["availability"] == before_inv["availability"]
    assert listings["W46538540"]["offers"][0]["listingDescription"] == before_offer["listingDescription"]
    assert listings["W46538540"]["offers"][0]["availableQuantity"] == before_offer["availableQuantity"]
    assert listings["W46538540"]["offers"][0]["pricingSummary"] == before_offer["pricingSummary"]
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_oos.json").read_text())
    assert any(entry.get("apply_status") == "write_failed" for entry in results["entries"])
    assert client.publishes  # restore republish


def test_map_pricing_is_preserved_on_offer_put(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    map_summary = {
        "price": {"value": "89.00", "currency": "USD"},
        "minimumAdvertisedPrice": {"value": "99.00", "currency": "USD"},
        "originallySoldForRetailPriceOn": "ON_EBAY",
        "strikethroughPrice": {"value": "129.00", "currency": "USD"},
    }
    listings["W46538540"]["offers"][0]["pricingSummary"] = deepcopy(map_summary)
    client = FakeEbay(listings)
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_map",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    payload = client.offer_puts[0]["json"]
    assert payload["pricingSummary"] == map_summary
    assert listings["W46538540"]["offers"][0]["pricingSummary"] == map_summary


def test_taxonomy_throw_skips_non_features_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")

    def boom(_cid):
        raise RuntimeError("taxonomy down")

    listings = _listings()
    listings["W465130444"] = {
        "inv": {
            "sku": "W465130444",
            "locale": "en_US",
            "condition": "NEW",
            "product": {
                "title": "Outdoor Pedestal Fan",
                "description": "fan",
                "aspects": {"Mounting Type": ["Wall"], "Features": ["Quiet"]},
                "imageUrls": ["https://i.ebayimg.com/x.jpg"],
            },
            "availability": {"shipToLocationAvailability": {"quantity": 2}},
        },
        "offers": [
            {
                "offerId": "offer-mount",
                "listingDescription": "fan",
                "categoryId": "123",
                "pricingSummary": {"price": {"value": "10.00", "currency": "USD"}},
                "availableQuantity": 2,
                "listing": {"listingId": "366000000001"},
            }
        ],
    }
    # Manifest has no W465130444 row; cover the three real keys via fixture SKUs + synthetic.
    args, parser = _args_sku_file(
        tmp_path,
        ["W3131P380454", "W3596P577884"],
        fix=True,
    )
    client = FakeEbay(listings)
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W3131P380454", "W3596P577884"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_tax",
        required_aspect_loader=boom,
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert listings["W3131P380454"]["inv"]["product"]["aspects"]["Fuel Type"] == ["Propane"]
    assert listings["W3596P577884"]["inv"]["product"]["aspects"]["Energy Efficiency"] == ["Energy Saving"]
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_tax.json").read_text())
    assert any(
        entry["sku"] == "W3131P380454" and entry["status"] == "required_aspects_unavailable"
        for entry in results["entries"]
    )
    assert any(
        entry["sku"] == "W3596P577884"
        and "Energy Efficiency" in (entry.get("token") or "")
        for entry in results["entries"]
    )
    # Features on the energy SKU can still strip.
    assert "Energy Efficient" not in listings["W3596P577884"]["inv"]["product"]["aspects"]["Features"]

    from src.utils import claim_strip as cs

    extra = [
        cs.ManifestRow(
            sku="W465130444",
            listing_id="366000000001",
            claim="wall",
            appears_in=["aspect:Mounting Type"],
            live_snippet="Mounting Type Wall",
            recommendation=cs.STRIP_NOW,
            raw_appears_in="aspect:Mounting Type",
        )
    ]
    plan = cs.plan_sku_claim_strip(
        "W465130444",
        extra,
        listings["W465130444"]["inv"],
        listings["W465130444"]["offers"][0],
        [{"type": "semantic_feature", "detail": "wall"}],
        required_aspects_available=False,
    )
    assert plan.results[0].status == cs.STATUS_REQUIRED_ASPECTS_UNAVAILABLE
    assert plan.aspects_after["Mounting Type"] == ["Wall"]


def test_publish_failure_restores_and_republishes(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    before_inv = deepcopy(listings["W46538540"]["inv"])
    before_offer = deepcopy(listings["W46538540"]["offers"][0])
    client = FakeEbay(listings)
    client.publish_fail_remaining = 1
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_pub",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert client.publishes == [before_offer["offerId"], before_offer["offerId"]]
    assert listings["W46538540"]["inv"]["product"]["aspects"] == before_inv["product"]["aspects"]
    assert listings["W46538540"]["offers"][0]["listingDescription"] == before_offer["listingDescription"]
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_pub.json").read_text())
    assert any(entry.get("apply_status") == "write_failed" for entry in results["entries"])
    assert all(entry["status"] != "restore_failed" for entry in results["entries"])


def test_restore_failure_is_restore_failed_and_stops_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args_sku_file(tmp_path, ["W46538540", "W2787P378160"], fix=True)
    listings = _listings()
    client = FakeEbay(listings)
    client.drift_after_first_write = True
    client.inventory_put_fail_on = {2}
    buf = io.StringIO()
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540", "W2787P378160"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_rfail",
        required_aspect_loader=lambda _cid: [],
        stdout=buf,
    )
    assert rc == 1
    assert "RESTORE_FAILED" in buf.getvalue()
    assert "STOP BATCH" in buf.getvalue()
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_rfail.json").read_text())
    assert any(entry["status"] == "restore_failed" for entry in results["entries"])
    assert all(entry["status"] != "failed_restored" for entry in results["entries"])
    assert all(entry["sku"] != "W2787P378160" for entry in results["entries"])


def test_w5368_live_readback_keeps_package_includes(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W5368P517794")
    listings = _listings()
    client = FakeEbay(listings)
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W5368P517794"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_ottoman",
        required_aspect_loader=lambda _cid: ["Type"],
        stdout=io.StringIO(),
    )
    assert rc == 0
    after_desc = listings["W5368P517794"]["offers"][0]["listingDescription"]
    assert "1 x Storage Ottoman" in after_desc
    assert "Storage Ottoman feels stable" not in after_desc
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_ottoman.json").read_text())
    assert any(entry["status"] == "stripped" for entry in results["entries"])
    assert all(entry.get("apply_status") != "write_failed" for entry in results["entries"])


def test_qty_change_between_fetches_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    client = FakeEbay(listings)

    def bump_qty(store):
        store["W46538540"]["inv"]["availability"]["shipToLocationAvailability"]["quantity"] = 3
        store["W46538540"]["offers"][0]["availableQuantity"] = 3

    client.after_first_pair = bump_qty
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_stale",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    assert client.inventory_puts == []
    assert client.offer_puts == []
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_stale.json").read_text())
    assert any(entry["status"] == "live_changed_between_fetches" for entry in results["entries"])


def test_fix_refuses_unset_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("EBAY_ENVIRONMENT", raising=False)
    args, parser = _args(tmp_path, fix=True)
    client = FakeEbay(_listings())
    buf = io.StringIO()
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_unset",
        required_aspect_loader=lambda _cid: [],
        stdout=buf,
    )
    assert rc == 2
    assert "REFUSE --fix" in buf.getvalue()
    assert client.puts == []


def test_price_change_between_fetches_aborts(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    client = FakeEbay(listings)

    def bump_price(store):
        store["W46538540"]["offers"][0]["pricingSummary"]["price"]["value"] = "1.00"

    client.after_first_pair = bump_price
    audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_price",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert client.inventory_puts == []
    results = json.loads((tmp_path / "claim_strip" / "live_20261022_price.json").read_text())
    assert any(entry["status"] == "live_changed_between_fetches" for entry in results["entries"])


def test_recheck_keeps_condition_fields_and_offer_extras(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args(tmp_path, fix=True, sku="W46538540")
    listings = _listings()
    extras = {
        "storeCategoryNames": ["Home > Tools"],
        "tax": {"applyTax": False, "vatPercentage": "0"},
        "quantityLimitPerBuyer": 5,
        "regulatory": {"repairScore": "0"},
        "secondaryCategoryId": "631",
        "lotSize": 1,
        "listingPolicies": {
            "fulfillmentPolicyId": "ful",
            "paymentPolicyId": "pay",
            "returnPolicyId": "ret",
        },
        "merchantLocationKey": "warehouse-main",
    }
    listings["W46538540"]["offers"][0].update(deepcopy(extras))

    def add_condition(store):
        store["W46538540"]["inv"]["conditionDescription"] = "New in box"
        store["W46538540"]["inv"]["conditionDescriptors"] = [
            {"name": "Condition", "values": [{"content": "NEW"}]}
        ]

    client = FakeEbay(listings)
    client.after_first_pair = add_condition
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_cond",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    inv_payload = client.inventory_puts[0]["json"]
    assert inv_payload["conditionDescription"] == "New in box"
    assert inv_payload["conditionDescriptors"][0]["name"] == "Condition"
    offer_payload = client.offer_puts[0]["json"]
    for key, value in extras.items():
        assert offer_payload[key] == value


def test_results_include_fetch_failed_and_live_listing_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("EBAY_ENVIRONMENT", "PRODUCTION")
    args, parser = _args_sku_file(
        tmp_path,
        ["W465P154347", "W2787P378160", "W46538540"],
        fix=False,
    )
    listings = _listings()
    client = FakeEbay(listings)
    client.fetch_errors = {"W465P154347": "timeout"}
    client.missing_skus = {"W2787P378160"}
    rc = audit.run_manifest_claim_strip(
        args,
        parser,
        ebay_client=client,
        db_conn=_memory_db(["W46538540"]),
        log_dir=tmp_path / "claim_strip",
        timestamp="20261022_miss",
        required_aspect_loader=lambda _cid: [],
        stdout=io.StringIO(),
    )
    assert rc == 0
    results = json.loads((tmp_path / "claim_strip" / "dryrun_20261022_miss.json").read_text())
    by_sku = {}
    for entry in results["entries"]:
        by_sku.setdefault(entry["sku"], []).append(entry["status"])
    assert "fetch_failed" in by_sku["W465P154347"]
    assert "live_listing_missing" in by_sku["W2787P378160"]
    assert any(entry["sku"] == "W46538540" for entry in results["entries"])
