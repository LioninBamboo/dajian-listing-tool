"""Safety and contract tests for the AquaRides description alignment runner."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "align_auto_descriptions", ROOT / "scripts" / "align_auto_descriptions.py"
)
align = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = align
SPEC.loader.exec_module(align)


def _listing(**overrides):
    data = {
        "sku": "SKU-1",
        "listing_id": "188683585596",
        "title": "Roof Rack Cargo Basket",
        "source_description": "source features",
        "attributes": {"Assembled Length (in.)": "50", "Assembled Width (in.)": "36", "Assembled Height (in.)": "5.1"},
        "specs": {},
        "aspects": {"Brand": ["AquaRides"], "Material": ["Steel"]},
    }
    data.update(overrides)
    return align.PublishedListing(**data)


def _valid_description():
    from src.services.auto_technical_prompt import build_auto_gold_description
    from src.utils.store_profile import StoreProfile

    return build_auto_gold_description(
        title="Roof Rack Cargo Basket",
        source_description="Steel cargo basket for roof racks. " * 20,
        attributes={"Material": "Steel"},
        specs={"Assembled Length (in.)": "50"},
        aspects={"Type": ["Cargo Basket"], "Material": ["Steel"], "Brand": ["AquaRides"]},
        profile=StoreProfile(
            brand_name="AquaRides",
            brand_tagline="PERFORMANCE AUTO PARTS",
            description_footer_line1="✦ Ships from US Warehouse ✦",
            quality_footer_marker="ships from",
            template_style="auto_technical",
            force_house_brand=True,
        ),
    )


def test_validate_description_enforces_live_acceptance_contract():
    result = align.validate_description(_valid_description())
    assert result["passed"] is True

    bad = _valid_description().replace("AQUARIDES", "AQUARIDES 产品规格")
    bad = bad.replace("Ships from US Warehouse", "Assembly Required")
    result = align.validate_description(bad)
    assert result["passed"] is False
    assert "contains_cjk_or_raw_chinese" in result["errors"]
    assert "contains_assembly_copy" in result["errors"]


def test_load_published_rows_filters_status_and_listing_id_with_parameters():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT, listing_id TEXT, title TEXT, description TEXT, "
        "attributes TEXT, specs TEXT, optimization TEXT, status TEXT)"
    )
    conn.executemany(
        "INSERT INTO collected_products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("published", "1", "T", "D", "{}", "{}", json.dumps({"aspects": {}}), "PUBLISHED"),
            ("no-id", "", "T", "D", "{}", "{}", "{}", "PUBLISHED"),
            ("ready", "2", "T", "D", "{}", "{}", "{}", "READY"),
        ],
    )
    rows = align.load_published_rows(conn)
    assert [(row.sku, row.listing_id) for row in rows] == [("published", "1")]


def test_execute_candidates_dry_run_never_calls_client():
    candidate = align.Candidate(_listing(), _valid_description(), align.validate_description(_valid_description()))

    class ForbiddenClient:
        def __getattr__(self, name):
            raise AssertionError(f"dry-run called live method: {name}")

    results = align.execute_candidates([candidate], client=ForbiddenClient(), apply=False, sleep_seconds=0.4)
    assert results[0].status == "ready"


def _furniture_live():
    return (
        '<div style="max-width:900px">AQUARIDES KEY FEATURES '
        "without overwhelming the room SPECIFICATIONS PACKAGE INCLUDES "
        "Ships from US Warehouse</div>"
    )


def test_execute_candidates_writes_local_optimization_after_verified_success():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, listing_id TEXT, title TEXT, "
        "description TEXT, attributes TEXT, specs TEXT, optimization TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO collected_products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "SKU-1",
            "188777275513",
            "T",
            "source",
            "{}",
            "{}",
            json.dumps({"description": "furniture shell", "aspects": {"Brand": ["AquaRides"]}}),
            "PUBLISHED",
        ),
    )
    listing = _listing(sku="SKU-1", listing_id="188777275513")
    candidate = align.Candidate(listing, _valid_description(), align.validate_description(_valid_description()))

    class FakeClient:
        def __init__(self):
            self.live = _furniture_live()

        def get_item_description_motors(self, listing_id):
            return self.live

        def revise_fixed_price_item_motors(self, listing_id, **kwargs):
            self.live = candidate.description
            return {"ack": "Warning"}

    results = align.execute_candidates(
        [candidate], client=FakeClient(), apply=True, sleep_seconds=0.4, conn=conn
    )
    assert results[0].status == "success"
    opt = json.loads(
        conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("SKU-1",)).fetchone()[0]
    )
    assert opt["description"] == candidate.description
    assert opt["aspects"]["Brand"] == ["AquaRides"]


def test_execute_candidates_skips_revise_when_live_already_gold_but_writes_local():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE collected_products (sku TEXT PRIMARY KEY, listing_id TEXT, optimization TEXT)"
    )
    conn.execute(
        "INSERT INTO collected_products VALUES (?, ?, ?)",
        ("SKU-1", "188777275513", json.dumps({"description": "old furniture"})),
    )
    gold = _valid_description()
    listing = _listing(sku="SKU-1", listing_id="188777275513")
    candidate = align.Candidate(listing, gold, align.validate_description(gold))

    class FakeClient:
        def __init__(self):
            self.revise_calls = 0

        def get_item_description_motors(self, listing_id):
            return gold

        def revise_fixed_price_item_motors(self, listing_id, **kwargs):
            self.revise_calls += 1
            raise AssertionError("already-gold live listing must not be revised")

    client = FakeClient()
    # Force the live read to look like gold chrome so the skip path is taken.
    gold_live = (
        "<div>AQUARIDES KEY FEATURES SPECIFICATIONS PACKAGE INCLUDES "
        "Ships from US Warehouse</div>"
        '<table><tr style="background:#1f2329;"><th>Attribute</th><th>Value</th></tr></table>'
    )

    class GoldClient(FakeClient):
        def get_item_description_motors(self, listing_id):
            return gold_live

    client = GoldClient()
    results = align.execute_candidates(
        [candidate], client=client, apply=True, sleep_seconds=0.4, conn=conn
    )
    assert results[0].status == "already_gold"
    assert client.revise_calls == 0
    opt = json.loads(
        conn.execute("SELECT optimization FROM collected_products WHERE sku=?", ("SKU-1",)).fetchone()[0]
    )
    assert opt["description"] == gold


def test_execute_candidates_reads_before_revises_only_description_and_reads_back():
    candidate = align.Candidate(_listing(), _valid_description(), align.validate_description(_valid_description()))

    class FakeClient:
        def __init__(self):
            self.calls = []

        def get_item_description_motors(self, listing_id):
            self.calls.append(("get", listing_id))
            if any(call[0] == "revise" for call in self.calls):
                return candidate.description
            return _furniture_live()

        def revise_fixed_price_item_motors(self, listing_id, **kwargs):
            self.calls.append(("revise", listing_id, kwargs))
            assert set(kwargs) == {"description", "aspects"}
            assert kwargs["description"] == candidate.description
            assert kwargs["aspects"] == candidate.listing.aspects
            return {"ack": "Warning"}

    client = FakeClient()
    results = align.execute_candidates([candidate], client=client, apply=True, sleep_seconds=0.4)
    assert results[0].status == "success"
    assert results[0].ack == "Warning"
    assert [call[0] for call in client.calls] == ["get", "revise", "get"]


def test_execute_candidates_does_not_count_failed_readback_as_success():
    candidate = align.Candidate(_listing(), _valid_description(), align.validate_description(_valid_description()))

    class FakeClient:
        def __init__(self):
            self.reads = 0

        def get_item_description_motors(self, listing_id):
            self.reads += 1
            return _furniture_live() if self.reads == 1 else "<div>broken</div>"

        def revise_fixed_price_item_motors(self, listing_id, **kwargs):
            return {"ack": "Success"}

    result = align.execute_candidates([candidate], client=FakeClient(), apply=True, sleep_seconds=0.4)[0]
    assert result.status == "verify_failed"
    assert result.error


def test_preflight_failure_is_fail_closed_before_live_execution(monkeypatch):
    candidate = align.Candidate(
        _listing(),
        "<div>too short</div>",
        align.validate_description("<div>too short</div>"),
    )
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(align, "execute_candidates", forbidden)
    results = [
        align.RunResult(
            sku=candidate.listing.sku,
            listing_id=candidate.listing.listing_id,
            status="preflight_failed",
            description_length=len(candidate.description),
            error="; ".join(candidate.validation["errors"]),
        )
    ]
    assert results[0].status == "preflight_failed"
    assert called is False
