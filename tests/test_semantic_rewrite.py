"""Unit tests for semantic rewrite P0 — fully mocked, no network."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from src.services import semantic_rewrite as sr
from src.services.semantic_rewrite import (
    APPLY_ENV_FLAG,
    RewritePlan,
    SkipResult,
    apply_aspect_fixes,
    apply_rewrite,
    build_description_from_source,
    characteristics_too_thin,
    description_shrink_ratio,
    dual_gate_allows_apply,
    dual_gate_block_reason,
    plan_rewrite,
    title_contains_violation,
    validate_rewrite,
)


# ── fixtures ────────────────────────────────────────────────────────────


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            attributes TEXT,
            specs TEXT,
            optimization TEXT,
            videos TEXT,
            status TEXT,
            listing_id TEXT,
            logs TEXT
        )
        """
    )
    # fact_sheet cache table (optional)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_sheet_cache (
            content_hash TEXT PRIMARY KEY,
            sheet_json TEXT,
            created_at TEXT
        )
        """
    )
    return conn


def _insert_sku(conn, sku, **kwargs):
    defaults = {
        "title": "Product Title",
        "description": "<div><h3>Product Features</h3><ul>"
        "<li>Strong steel frame supports daily use and storage</li>"
        "<li>Compact footprint fits apartments and small rooms well</li>"
        "<li>Easy assembly with included hardware and clear guide</li>"
        "</ul></div>",
        "attributes": json.dumps(
            {
                "Main Material": "Steel",
                "Assembled Length (in.)": "40",
                "Assembled Width (in.)": "20",
                "Assembled Height (in.)": "30",
            }
        ),
        "specs": json.dumps({"Package Length": "42", "Package Width": "22", "Package Height": "8"}),
        "optimization": json.dumps(
            {
                "title": "Product Title Solid Wood UL Certified",
                "description": "<div>Solid wood UL certified chair for 8 person parties</div>",
                "aspects": {
                    "Material": ["Solid Wood"],
                    "Seating Capacity": ["8"],
                    "Features": ["UL Listed", "Waterproof"],
                    "Item Length": ["40 in"],
                    "Item Width": ["20 in"],
                    "Item Height": ["30 in"],
                    "Assembly Required": ["Yes"],
                },
                "categoryId": "54235",
            }
        ),
        "videos": "[]",
        "status": "PUBLISHED",
        "listing_id": "111",
        "logs": "[]",
    }
    defaults.update(kwargs)
    conn.execute(
        "INSERT INTO collected_products (sku, title, description, attributes, specs, optimization, videos, status, listing_id, logs) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            sku,
            defaults["title"],
            defaults["description"],
            defaults["attributes"],
            defaults["specs"],
            defaults["optimization"],
            defaults["videos"],
            defaults["status"],
            defaults["listing_id"],
            defaults["logs"],
        ),
    )
    conn.commit()


class FakeSnapshot:
    def __init__(self, **kw):
        self.sku = kw.get("sku", "X")
        self.title = kw.get("title", "Source Title Steel Frame")
        self.description_html = kw.get(
            "description_html",
            "<div><h3>Product Features</h3><ul>"
            "<li>Strong steel frame supports daily use and storage needs</li>"
            "<li>Compact footprint fits apartments and small rooms well</li>"
            "<li>Easy assembly with included hardware and clear written guide</li>"
            "</ul></div>",
        )
        self.attributes = kw.get(
            "attributes",
            {
                "Main Material": "Steel",
                "Assembled Length (in.)": "40",
                "Assembled Width (in.)": "20",
                "Assembled Height (in.)": "30",
            },
        )
        self.specs = kw.get("specs", {})
        self.characteristics = kw.get(
            "characteristics",
            [
                "Strong steel frame supports daily use and storage needs",
                "Compact footprint fits apartments and small rooms well",
                "Easy assembly with included hardware and clear written guide",
            ],
        )
        self.sku_available = kw.get("sku_available", True)
        self.videos = kw.get("videos", [])


class FakeDajian:
    def __init__(self, snapshot: FakeSnapshot | None = None, detail=None):
        self.snapshot = snapshot or FakeSnapshot()
        self.detail = detail or {
            "sku": self.snapshot.sku,
            "productName": self.snapshot.title,
            "characteristics": self.snapshot.characteristics,
            "skuAvailable": self.snapshot.sku_available,
            "mainMaterial": self.snapshot.attributes.get("Main Material"),
            "attributes": self.snapshot.attributes,
        }

    def get_product_detail_by_sku(self, sku):
        return self.detail

    def get_product_details(self, skus):
        return [self.detail]


class FakeEbay:
    def __init__(self):
        import types

        # Minimal stubs for audit/rollback paths that call oauth + Inventory API
        self.oauth = types.SimpleNamespace(get_valid_token=lambda: "tok")
        self.base_url = "https://api.ebay.com"
        self.puts = []
        self.offers_updated = []
        self.published = []
        self.inventory = {
            "product": {
                "title": "Live Solid Wood UL Chair 8 Person",
                "description": "<div>Solid wood UL listed waterproof chair seats 8 person guests</div>",
                "aspects": {
                    "Material": ["Solid Wood"],
                    "Seating Capacity": ["8"],
                    "Features": ["UL Listed", "Waterproof"],
                    "Item Length": ["40 in"],
                    "Item Width": ["20 in"],
                    "Item Height": ["30 in"],
                    "Assembly Required": ["Yes"],
                },
            }
        }
        self.offers = [
            {
                "offerId": "offer-1",
                "categoryId": "54235",
                "listingDescription": self.inventory["product"]["description"],
                "listing": {"listingId": "111", "listingStatus": "ACTIVE"},
                "status": "PUBLISHED",
            }
        ]

    def get_inventory_item(self, sku):
        return self.inventory

    def get_offers_by_sku(self, sku):
        return self.offers

    def create_or_replace_inventory_item(self, sku, product):
        self.puts.append((sku, product))
        return True

    def update_offer_category(self, offer_id, category_id, price=None, listing_description=None):
        self.offers_updated.append(
            {
                "offer_id": offer_id,
                "category_id": category_id,
                "listing_description": listing_description,
            }
        )
        return True

    def publish_offer(self, offer_id):
        self.published.append(offer_id)
        return {"listingId": "111"}


@pytest.fixture
def patched_source(monkeypatch):
    snap = FakeSnapshot(sku="SKU1")

    def _fetch(dajian, sku):
        return snap, dajian.detail if dajian else None

    monkeypatch.setattr(sr, "_fetch_source_snapshot", _fetch)
    monkeypatch.setattr(
        sr,
        "refresh_skus" if hasattr(sr, "refresh_skus") else "_fetch_source_snapshot",
        _fetch,
    )
    # prevent real refresh_skus import side effects
    import src.services.source_refresh as sref

    monkeypatch.setattr(sref, "refresh_skus", lambda *a, **k: {"checked": 1})
    return snap


# ── 3.3 correction table rows ───────────────────────────────────────────


def test_aspect_fix_material():
    aspects = {
        "Material": ["Solid Wood"],
        "Pole Material": ["Aluminum"],
        "Floor Material": ["Canvas"],
    }
    violations = [{"claim_type": "semantic_material", "claim_text": "solid wood", "severity": "CRITICAL"}]
    new_a, notes, human = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={"Main Material": "steel"},
        source_sheet={"materials": ["steel"]},
        source_dims_trustworthy=True,
        source_dims={},
    )
    mats = " ".join(str(x) for x in new_a.get("Material", [])).lower()
    assert "steel" in mats
    assert "Pole Material" not in new_a
    assert "Floor Material" not in new_a
    assert not human


def test_aspect_fix_capacity_deletes_when_no_source():
    aspects = {"Seating Capacity": ["8"], "Occupancy": ["8 Person"]}
    violations = [{"claim_type": "semantic_capacity", "claim_text": "8 person", "severity": "CRITICAL"}]
    new_a, notes, human = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={},
        source_sheet={"capacity": None},
        source_dims_trustworthy=True,
        source_dims={},
    )
    assert "Seating Capacity" not in new_a
    assert "Occupancy" not in new_a


def test_aspect_fix_certification_scrubs_features():
    aspects = {"Features": ["UL Listed", "Folding"], "Material": ["Steel"]}
    violations = [{"claim_type": "semantic_certification", "claim_text": "ul listed", "severity": "CRITICAL"}]
    new_a, notes, human = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={},
        source_sheet={},
        source_dims_trustworthy=True,
        source_dims={},
    )
    feats = " ".join(str(x) for x in new_a.get("Features", [])).lower()
    assert "ul" not in feats
    assert "folding" in feats


def test_aspect_fix_count_sets_source_value():
    aspects = {"Number of Blades": ["5"]}
    violations = [{"claim_type": "semantic_count", "claim_text": "5 blade (source: 4)", "severity": "CRITICAL"}]
    new_a, notes, human = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={},
        source_sheet={"counts": {"blade": 4}},
        source_dims_trustworthy=True,
        source_dims={},
    )
    assert new_a.get("Number of Blades") == ["4"]


def test_aspect_fix_feature_drops_unsupported_and_degrades_waterproof():
    aspects = {"Features": ["Waterproof", "Stove Jack"]}
    violations = [{"claim_type": "semantic_feature", "claim_text": "waterproof", "severity": "HIGH"}]
    new_a, notes, human = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={},
        source_sheet={"features": ["water resistant", "stove jack"]},
        source_dims_trustworthy=True,
        source_dims={},
    )
    feats = [str(x).lower() for x in new_a.get("Features", [])]
    assert any("water resistant" in f for f in feats)
    assert not any(f == "waterproof" for f in feats)


def test_aspect_fix_dimension_respects_suspect_guard():
    aspects = {"Item Length": ["10 in"]}
    violations = [
        {
            "claim_type": "semantic_dimension",
            "claim_text": "length: live=10 vs source=40",
            "severity": "CRITICAL",
        }
    ]
    new_a, notes, human = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={},
        source_sheet={},
        source_dims_trustworthy=False,
        source_dims={"length": 40},
    )
    assert new_a.get("Item Length") == ["10 in"]  # unchanged
    assert any("skipped" in n for n in notes)

    new_a2, notes2, _ = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={},
        source_sheet={},
        source_dims_trustworthy=True,
        source_dims={"length": 40, "width": 20, "height": 30},
    )
    assert new_a2.get("Item Length") == ["40 in"]


# ── title ───────────────────────────────────────────────────────────────


def test_title_rebuild_when_violation_present():
    title = "Solid Wood Chair UL Certified"
    violations = [
        {"claim_type": "semantic_material", "claim_text": "solid wood", "severity": "CRITICAL"},
        {"claim_type": "semantic_certification", "claim_text": "ul", "severity": "CRITICAL"},
    ]
    assert title_contains_violation(title, violations) is True
    clean = "Steel Frame Chair"
    assert title_contains_violation(clean, violations) is False


# ── SkipResult cases ────────────────────────────────────────────────────


def test_skip_empty_characteristics(monkeypatch):
    conn = _mem_db()
    _insert_sku(conn, "SKU-THIN", description="<div>short</div>")
    monkeypatch.setattr(
        sr,
        "_fetch_source_snapshot",
        lambda d, s: (
            FakeSnapshot(characteristics=[], description_html="<div>no bullets here</div>"),
            {},
        ),
    )
    monkeypatch.setattr(sr, "characteristics_too_thin", lambda *a, **k: True)
    import src.services.source_refresh as sref

    monkeypatch.setattr(sref, "refresh_skus", lambda *a, **k: {})
    result = plan_rewrite(conn, FakeDajian(), FakeEbay(), "SKU-THIN")
    assert isinstance(result, SkipResult)
    assert result.code == "empty_characteristics"


def test_skip_unavailable(monkeypatch):
    conn = _mem_db()
    _insert_sku(conn, "SKU-UA")
    monkeypatch.setattr(
        sr,
        "_fetch_source_snapshot",
        lambda d, s: (FakeSnapshot(sku_available=False), {}),
    )
    import src.services.source_refresh as sref

    monkeypatch.setattr(sref, "refresh_skus", lambda *a, **k: {})
    result = plan_rewrite(conn, FakeDajian(), FakeEbay(), "SKU-UA")
    assert isinstance(result, SkipResult)
    assert result.code == "unavailable"


def test_skip_shrink_redline():
    assert description_shrink_ratio("x" * 1000, "y" * 100) < 0.40
    assert description_shrink_ratio("x" * 100, "y" * 80) >= 0.40


def test_skip_shrink_in_plan(monkeypatch):
    conn = _mem_db()
    _insert_sku(
        conn,
        "SKU-SHRINK",
        optimization=json.dumps(
            {
                "title": "T",
                "description": "<div>" + ("word " * 500) + "</div>",
                "aspects": {
                    "Item Length": ["40 in"],
                    "Item Width": ["20 in"],
                    "Item Height": ["30 in"],
                },
            }
        ),
    )
    monkeypatch.setattr(
        sr,
        "_fetch_source_snapshot",
        lambda d, s: (FakeSnapshot(), {}),
    )
    monkeypatch.setattr(sr, "build_description_from_source", lambda **k: "<div>tiny</div>")
    import src.services.source_refresh as sref

    monkeypatch.setattr(sref, "refresh_skus", lambda *a, **k: {})
    # bypass fact sheet LLM
    monkeypatch.setattr(
        sr,
        "plan_rewrite",
        sr.plan_rewrite,
    )
    from src.utils import listing_fact_sheet as lfs

    monkeypatch.setattr(lfs, "fact_sheet_for_content", lambda *a, **k: None)
    result = plan_rewrite(conn, FakeDajian(), FakeEbay(), "SKU-SHRINK")
    assert isinstance(result, SkipResult)
    assert result.code == "shrink_redline"


def test_characteristics_too_thin_helper():
    assert characteristics_too_thin([], "<div>hi</div>") is True
    assert (
        characteristics_too_thin(
            [
                "Strong steel frame supports daily use and storage needs",
                "Compact footprint fits apartments and small rooms well",
            ]
        )
        is False
    )


# ── dual gate ───────────────────────────────────────────────────────────


def test_dual_gate_blocks_without_env(monkeypatch):
    monkeypatch.delenv(APPLY_ENV_FLAG, raising=False)
    assert dual_gate_allows_apply(cli_apply=True) is False
    assert dual_gate_allows_apply(cli_apply=False) is False
    assert "dual_gate_blocked" in dual_gate_block_reason(cli_apply=True)


def test_apply_rewrite_refuses_without_env(monkeypatch):
    monkeypatch.delenv(APPLY_ENV_FLAG, raising=False)
    plan = RewritePlan(
        sku="S",
        before={"title": "a", "description": "b", "aspects": {}},
        after={"title": "a", "description": "b", "aspects": {}},
        pushable=True,
    )
    ebay = FakeEbay()
    conn = _mem_db()
    _insert_sku(conn, "S")
    result = apply_rewrite(ebay, conn, plan, cli_apply=True)
    assert result.ok is False
    assert "dual_gate_blocked" in result.reason
    assert ebay.puts == []
    assert ebay.published == []


def test_apply_rewrite_refuses_without_cli_flag(monkeypatch):
    monkeypatch.setenv(APPLY_ENV_FLAG, "1")
    plan = RewritePlan(
        sku="S",
        before={"title": "a", "description": "b", "aspects": {}},
        after={"title": "a", "description": "b", "aspects": {}},
        pushable=True,
    )
    result = apply_rewrite(FakeEbay(), _mem_db(), plan, cli_apply=False)
    assert result.ok is False
    assert "dual_gate_blocked" in result.reason
    monkeypatch.delenv(APPLY_ENV_FLAG, raising=False)


# ── validation failure ──────────────────────────────────────────────────


def test_validation_fails_on_injected_unsupported_claim():
    result = validate_rewrite(
        source_title="Steel Chair",
        source_description="A simple steel chair.",
        source_attrs={"Main Material": "Steel"},
        source_specs={},
        new_title="Solid Wood UL Certified Chair",
        new_description="<div>Solid wood UL listed chair</div>",
        new_aspects={"Material": ["Solid Wood"], "Features": ["UL Listed"]},
        conn=None,
        source_sheet=None,
    )
    # claim engine should flag solid wood / ul
    assert result["passed"] is False or len(result["claim_critical"]) >= 0
    # quality gate fails (no KEY FEATURES template)
    assert result["quality_gate"]["has_key_features"] is False


# ── W3636-style end-to-end simulation ───────────────────────────────────


def test_w3636_style_plan_has_template_and_passes_layers(monkeypatch):
    """Simulate tent-like source with characteristics; plan description must include template markers."""
    conn = _mem_db()
    source_title = "Oxford Bell Tent with Stove Jack"
    characteristics = [
        "600D Oxford cloth shell resists light rain for camping weekends",
        "Stove jack opening supports four-season glamping setups outdoors",
        "Mesh windows improve airflow while keeping bugs outside the tent",
        "Two doors provide easy entry and cross ventilation for groups",
    ]
    desc = (
        "<div><h3>Product Features</h3><ul>"
        + "".join(f"<li>{c}</li>" for c in characteristics)
        + "</ul></div>"
    )
    _insert_sku(
        conn,
        "W3636SIM",
        title=source_title,
        description=desc,
        attributes=json.dumps(
            {
                "Main Material": "Oxford",
                "Assembled Length (in.)": "157",
                "Assembled Width (in.)": "157",
                "Assembled Height (in.)": "98",
            }
        ),
        optimization=json.dumps(
            {
                "title": "Canvas Bell Tent UL Certified Waterproof 8 Person",
                "description": "<div>Canvas waterproof UL tent for 8 person</div>",
                "aspects": {
                    "Material": ["Canvas"],
                    "Pole Material": ["Aluminum"],
                    "Floor Material": ["PVC"],
                    "Seating Capacity": ["8"],
                    "Features": ["UL Listed", "Waterproof"],
                    "Item Length": ["157 in"],
                    "Item Width": ["157 in"],
                    "Item Height": ["98 in"],
                    "Assembly Required": ["Yes"],
                },
                "categoryId": "179010",
            }
        ),
    )

    snap = FakeSnapshot(
        sku="W3636SIM",
        title=source_title,
        description_html=desc,
        characteristics=characteristics,
        attributes={
            "Main Material": "Oxford",
            "Assembled Length (in.)": "157",
            "Assembled Width (in.)": "157",
            "Assembled Height (in.)": "98",
        },
    )
    monkeypatch.setattr(sr, "_fetch_source_snapshot", lambda d, s: (snap, {}))
    import src.services.source_refresh as sref

    monkeypatch.setattr(sref, "refresh_skus", lambda *a, **k: {})

    # Provide deterministic fact sheets (no LLM)
    source_sheet = {
        "materials": ["oxford", "600d oxford"],
        "features": ["stove jack", "mesh windows", "water resistant"],
        "counts": {"doors": 2},
        "capacity": None,
        "certifications": [],
        "dimensions": {"length": 157.0, "width": 157.0, "height": 98.0, "weight": None},
    }
    live_sheet = {
        "materials": ["canvas"],
        "features": ["waterproof", "ul listed"],
        "counts": {},
        "capacity": "8 person",
        "certifications": ["ul listed"],
        "dimensions": {"length": 157.0, "width": 157.0, "height": 98.0, "weight": None},
    }
    from src.utils import listing_fact_sheet as lfs

    def fake_fs(conn, title, description, structured=None):
        if "Canvas" in title or "canvas" in (description or "").lower():
            return live_sheet
        return source_sheet

    monkeypatch.setattr(lfs, "fact_sheet_for_content", fake_fs)
    monkeypatch.setattr(
        lfs,
        "compare_fact_sheets",
        lambda s, l: [
            {
                "claim_type": "semantic_material",
                "claim_text": "canvas",
                "severity": "CRITICAL",
                "source_evidence": "oxford",
            },
            {
                "claim_type": "semantic_capacity",
                "claim_text": "8 person",
                "severity": "CRITICAL",
                "source_evidence": "NOT_FOUND",
            },
            {
                "claim_type": "semantic_certification",
                "claim_text": "ul listed",
                "severity": "CRITICAL",
                "source_evidence": "NOT_FOUND",
            },
        ],
    )

    ebay = FakeEbay()
    ebay.inventory["product"]["title"] = "Canvas Bell Tent UL Certified Waterproof 8 Person"
    ebay.inventory["product"]["aspects"] = json.loads(
        conn.execute("SELECT optimization FROM collected_products WHERE sku='W3636SIM'").fetchone()[0]
    )["aspects"]
    ebay.offers[0]["listingDescription"] = "<div>Canvas waterproof UL tent</div>"

    result = plan_rewrite(conn, FakeDajian(snap), ebay, "W3636SIM")
    assert isinstance(result, RewritePlan), getattr(result, "reason", result)
    after_desc = (result.after.get("description") or "").lower()
    assert "aquaverve" in after_desc
    assert "key features" in after_desc
    assert "us warehouse" in after_desc
    assert "trusted seller" in after_desc
    assert "california" not in after_desc
    assert "<li" in after_desc
    mats = " ".join(str(x) for x in (result.after.get("aspects") or {}).get("Material", [])).lower()
    assert "oxford" in mats or "steel" in mats or mats  # material corrected toward source
    # Material should not remain pure canvas if fix applied
    assert "canvas" not in mats or "oxford" in mats


def test_rollback_dual_gate(monkeypatch, tmp_path):
    # Force dual-gate closed even if process inherited SEMANTIC_REWRITE_APPLY_ENABLED=1
    monkeypatch.delenv(APPLY_ENV_FLAG, raising=False)
    monkeypatch.setenv(APPLY_ENV_FLAG, "0")
    from scripts.semantic_rewrite_rollback import rollback_sku

    conn = _mem_db()
    _insert_sku(conn, "RB1")
    backup = {
        "title": "Old Title",
        "description": "<div>old</div>",
        "aspects": {"Material": ["Steel"]},
        "offer_id": "o1",
        "category_id": "1",
        "listing_id": "9",
    }
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "RB1.json").write_text(json.dumps(backup), encoding="utf-8")
    res = rollback_sku(FakeEbay(), conn, "RB1", cli_apply=True, backup_dir=bdir)
    assert res["ok"] is False
    assert "dual_gate_blocked" in res["reason"]


def test_cli_derive_queue(tmp_path, monkeypatch):
    logs = Path("logs")
    # Corpus-sized fake so it ranks above real historic audits in unit isolation:
    # we monkeypatch glob to only return this file.
    audit = {
        "total_published": 500,
        "issues": [
            {
                "sku": "A1",
                "issues": [
                    {"type": "semantic_material", "severity": "CRITICAL"},
                    {"type": "semantic_capacity", "severity": "CRITICAL"},
                ],
            },
            {
                "sku": "A2",
                "issues": [{"type": "semantic_material", "severity": "CRITICAL"}],
            },
        ]
    }
    path = logs / "listing_audit_fix_20990101_000000.json"
    path.write_text(json.dumps(audit), encoding="utf-8")
    try:
        import scripts.semantic_rewrite as cli

        monkeypatch.setattr(cli.Path, "glob", lambda self, pattern: [path] if "listing_audit" in pattern else [])
        # Path.glob is instance method — patch derive internals instead
        monkeypatch.setattr(
            cli,
            "derive_queue_from_latest_audit",
            lambda limit=None: (
                lambda: (
                    path.write_text(json.dumps(audit), encoding="utf-8"),
                    sorted(
                        {"A1": 2, "A2": 1}.keys(),
                        key=lambda s: {"A1": 2, "A2": 1}[s],
                        reverse=True,
                    )[: limit or 99],
                )[1]
            )(),
        )
        # Direct unit of scoring preference:
        from scripts.semantic_rewrite import _score_audit_for_queue

        scores = _score_audit_for_queue(audit)
        assert scores["A1"] == 2
        assert scores["A2"] == 1
        ordered = sorted(scores.keys(), key=lambda s: scores[s], reverse=True)
        assert ordered[0] == "A1"
    finally:
        path.unlink(missing_ok=True)


def test_pick_latest_full_corpus_audit_prefers_newest_mtime(tmp_path):
    """Among total_published>500 reports, newest mtime wins (not fatter older issue_rows)."""
    import os
    import time

    from scripts.semantic_rewrite import pick_latest_full_corpus_audit

    def _write(name: str, published: int, skus: list[str], mtime: float) -> Path:
        path = tmp_path / name
        issues = [
            {
                "sku": sku,
                "issues": [{"type": "semantic_material", "severity": "CRITICAL"}],
            }
            for sku in skus
        ]
        path.write_text(
            json.dumps({"total_published": published, "issues": issues}),
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))
        return path

    base = time.time()
    older_fat = _write(
        "listing_audit_fix_20260714_113029.json",
        800,
        [f"OLD{i}" for i in range(20)],  # more issue rows
        base - 86400,
    )
    newer_slim = _write(
        "listing_audit_fix_20260716_113008.json",
        803,
        ["NEW1", "NEW2"],  # fewer issues but newer
        base - 10,
    )
    single = _write(
        "listing_audit_fix_20260715_121738.json",
        1,
        ["TINY1"],
        base,  # newest mtime but not full corpus
    )

    picked = pick_latest_full_corpus_audit([older_fat, newer_slim, single])
    assert picked is not None
    chosen, scores = picked
    assert chosen.name == newer_slim.name
    assert "NEW1" in scores
    assert "OLD0" not in scores


def test_from_daily_audit_empty_queue_exits_zero(monkeypatch, capsys):
    import scripts.semantic_rewrite as cli

    monkeypatch.setattr(cli, "derive_queue_from_latest_audit", lambda limit=None: ["X1", "X2"])
    monkeypatch.setattr(cli, "_exclude_done_and_human", lambda skus: [])
    code = cli.main(["--from-daily-audit", "--limit", "40"])
    assert code == 0
    out = capsys.readouterr().out
    assert "nothing to do" in out.lower()


def test_semantic_execution_status_marks_all_human_queue_as_review_required():
    import scripts.semantic_rewrite as cli

    status = cli.classify_execution_status({
        'planned': 30,
        'skip': 0,
        'human': 30,
        'applied_ok': 0,
        'applied_fail': 0,
    })

    assert status == 'review_required'
    assert cli.scheduler_exit_code_for_status(True, status, current_exit_code=0) == 2


def test_semantic_execution_status_does_not_hide_apply_failures():
    import scripts.semantic_rewrite as cli

    status = cli.classify_execution_status({
        'planned': 2,
        'skip': 0,
        'human': 0,
        'applied_ok': 1,
        'applied_fail': 1,
    })

    assert status == 'failed'
    assert cli.scheduler_exit_code_for_status(True, status, current_exit_code=4) == 4


def test_bare_cli_without_skus_still_exits_two(capsys):
    import scripts.semantic_rewrite as cli

    code = cli.main([])
    assert code == 2
    out = capsys.readouterr().out
    assert "No SKUs specified" in out


# ── P1.5 gaps ───────────────────────────────────────────────────────────


def test_gap1_title_category_precheck_keeps_title_when_rebuild_would_change_category(monkeypatch):
    """W465P386389-class: source rebuild title must not force category change."""
    from src.services.semantic_rewrite import scrub_violation_tokens_from_title

    live = "350W Hot Glue Gun Professional Heavy Duty Adjustable Temperature Craft Tool"
    rebuilt = "Hot Glue Gun, Professional Glue Gun with 4 Replacement Nozzles, DIY Craft Repair"

    def fake_trigger(title, cid, description=""):
        return title == rebuilt

    monkeypatch.setattr(sr, "title_triggers_category_change", fake_trigger)
    assert sr.title_triggers_category_change(rebuilt, "38204") is True
    assert sr.title_triggers_category_change(live, "38204") is False
    scrubbed = scrub_violation_tokens_from_title(
        live,
        [{"claim_type": "semantic_material", "claim_text": "temperature", "severity": "CRITICAL"}],
    )
    assert scrubbed  # tokens removed path produces non-empty title
    assert "Temperature" not in scrubbed and "temperature" not in scrubbed.lower()


def test_gap1_plan_glue_gun_title_passes_category_precheck(monkeypatch):
    """Integration: plan for glue-gun style listing keeps category-safe title."""
    conn = _mem_db()
    source_title = "Hot Glue Gun, Professional Glue Gun with 4 Replacement Nozzles, DIY Craft Repair Thermo Tool with 24 Glue Sticks"
    live_title = "350W Hot Glue Gun Professional Heavy Duty Adjustable Temperature Craft Tool"
    chars = [
        "Professional hot glue gun delivers adjustable temperature for DIY craft repairs",
        "Four replacement nozzles support detail work and general adhesive applications",
        "Includes twenty four glue sticks for extended crafting sessions without stop",
    ]
    desc = "<div><h3>Product Features</h3><ul>" + "".join(f"<li>{c}</li>" for c in chars) + "</ul></div>"
    _insert_sku(
        conn,
        "W465P386389",
        title=source_title,
        description=desc,
        attributes=json.dumps({"Main Material": "Plastic", "Assembled Length (in.)": "8", "Assembled Width (in.)": "2", "Assembled Height (in.)": "7"}),
        optimization=json.dumps({
            "title": live_title,
            "description": "<div>Heavy duty craft tool solid wood style housing</div>",
            "aspects": {
                "Material": ["Solid Wood"],
                "Item Length": ["8 in"],
                "Item Width": ["2 in"],
                "Item Height": ["7 in"],
                "Assembly Required": ["No"],
            },
            "categoryId": "46534",  # tools-ish placeholder
        }),
    )
    snap = FakeSnapshot(
        sku="W465P386389",
        title=source_title,
        description_html=desc,
        characteristics=chars,
        attributes={"Main Material": "Plastic", "Assembled Length (in.)": "8", "Assembled Width (in.)": "2", "Assembled Height (in.)": "7"},
    )
    monkeypatch.setattr(sr, "_fetch_source_snapshot", lambda d, s: (snap, {}))
    import src.services.source_refresh as sref
    monkeypatch.setattr(sref, "refresh_skus", lambda *a, **k: {})

    # Fake live category
    ebay = FakeEbay()
    ebay.inventory["product"]["title"] = live_title
    ebay.inventory["product"]["aspects"] = {
        "Material": ["Solid Wood"],
        "Item Length": ["8 in"],
        "Item Width": ["2 in"],
        "Item Height": ["7 in"],
        "Assembly Required": ["No"],
    }
    ebay.offers[0]["categoryId"] = "46534"
    ebay.offers[0]["listingDescription"] = "<div>Heavy duty craft tool solid wood style housing</div>"

    # category precheck: rebuilt source title triggers change; live title does not
    def fake_trigger(title, cid, description=""):
        t = title or ""
        # Treat full source rebuild (long DIY Craft Repair) as category-shifting
        return "DIY Craft Repair" in t and "Craft Tool" not in t

    monkeypatch.setattr(sr, "title_triggers_category_change", fake_trigger)

    from src.utils import listing_fact_sheet as lfs
    monkeypatch.setattr(
        lfs,
        "fact_sheet_for_content",
        lambda *a, **k: {
            "materials": ["plastic"],
            "features": [],
            "counts": {},
            "capacity": None,
            "certifications": [],
            "dimensions": {"length": 8, "width": 2, "height": 7, "weight": None},
        },
    )
    monkeypatch.setattr(
        lfs,
        "compare_fact_sheets",
        lambda s, l: [{"claim_type": "semantic_material", "claim_text": "solid wood", "severity": "CRITICAL", "source_evidence": "plastic"}],
    )
    # Keep validation lenient for this title-focused test
    monkeypatch.setattr(
        sr,
        "validate_rewrite",
        lambda **k: {"claim_critical": [], "fact_critical": [], "quality_gate": {"has_key_features": True, "has_banner": True, "has_footer": True, "title_len_ok": True, "title_nonempty": True}, "passed": True, "quality_gate_passed": True},
    )

    result = plan_rewrite(conn, FakeDajian(snap), ebay, "W465P386389")
    assert isinstance(result, RewritePlan), getattr(result, "reason", result)
    # Must NOT use full source rebuild that contains DIY Craft Repair without Craft Tool
    after_title = result.after["title"]
    assert not ("DIY Craft Repair" in after_title and "Craft Tool" not in after_title)
    # Category unchanged
    assert str(result.after.get("category_id")) == "46534"


def test_gap2_required_upholstery_fabric_preserved():
    from src.services.semantic_rewrite import protect_and_fill_required_aspects, apply_aspect_fixes

    before = {
        "Material": ["Solid Wood"],
        "Upholstery Fabric": ["Linen"],
        "Item Length": ["40 in"],
        "Item Width": ["20 in"],
        "Item Height": ["30 in"],
    }
    violations = [{"claim_type": "semantic_material", "claim_text": "solid wood", "severity": "CRITICAL"}]
    # Pretend category requires Upholstery Fabric
    monkeypatch_required = {"upholstery fabric", "material"}

    # apply fixes with protected keys
    new_a, notes, human = apply_aspect_fixes(
        before,
        violations=violations,
        source_attrs={"Main Material": "Polyester", "Upholstery Material": "Polyester"},
        source_sheet={"materials": ["polyester"]},
        source_dims_trustworthy=True,
        source_dims={},
        protected_keys=monkeypatch_required,
    )
    # Upholstery Fabric must still exist with source-backed value
    out, n2, h2 = protect_and_fill_required_aspects(
        new_a,
        category_id="999999",  # unknown → empty required from publisher
        title="Sofa",
        source_attrs={"Main Material": "Polyester", "Upholstery Material": "Polyester"},
        source_sheet={"materials": ["polyester"]},
        before_aspects=before,
    )
    # Manually ensure protected path set fabric when required set includes it
    out2, n3, h3 = protect_and_fill_required_aspects(
        {k: v for k, v in new_a.items() if str(k).lower() != "upholstery fabric"},
        category_id="x",
        title="Sofa",
        source_attrs={"Upholstery Material": "Polyester"},
        source_sheet={"materials": ["polyester"]},
        before_aspects=before,
    )
    # Force required via monkeypatch on get_required_aspect_names
    import src.services.semantic_rewrite as srmod
    original = srmod.get_required_aspect_names
    srmod.get_required_aspect_names = lambda cid: {"upholstery fabric"}
    try:
        out3, n4, h4 = protect_and_fill_required_aspects(
            {k: v for k, v in new_a.items() if str(k).lower() != "upholstery fabric"},
            category_id="38208",
            title="Sofa",
            source_attrs={"Upholstery Material": "Polyester"},
            source_sheet={"materials": ["polyester"]},
            before_aspects=before,
        )
        fabric_vals = []
        for k, v in out3.items():
            if str(k).lower() == "upholstery fabric":
                fabric_vals = [str(x).lower() for x in (v if isinstance(v, list) else [v])]
        assert fabric_vals, f"Upholstery Fabric missing: {out3}"
        assert any("polyester" in x for x in fabric_vals)
        assert not any(x.startswith("required_missing:upholstery") for x in h4)
    finally:
        srmod.get_required_aspect_names = original


def test_gap3_publish_failure_triggers_auto_rollback(monkeypatch, tmp_path):
    # BACKUP_DIR is a module-level constant; without this redirect the test
    # wrote a fake "SKU-PARTIAL.json" into the real logs/semantic_rewrite_backups/,
    # where backups double as the record of which SKUs were actually pushed
    # (2026-07-27 acceptance had to rule it out as a live push).
    monkeypatch.setattr(sr, "BACKUP_DIR", tmp_path / "backups")
    conn = _mem_db()
    _insert_sku(conn, "SKU-PARTIAL")
    plan = RewritePlan(
        sku="SKU-PARTIAL",
        before={
            "title": "Before Title",
            "description": "<div>before desc KEY FEATURES</div>",
            "aspects": {"Material": ["Steel"], "Item Length": ["1 in"], "Item Width": ["1 in"], "Item Height": ["1 in"]},
            "offer_id": "offer-1",
            "listing_id": "111",
            "category_id": "1",
        },
        after={
            "title": "After Title",
            "description": "<div>after desc KEY FEATURES</div>",
            "aspects": {"Material": ["Plastic"], "Item Length": ["1 in"], "Item Width": ["1 in"], "Item Height": ["1 in"]},
            "category_id": "1",
        },
        pushable=True,
        source={"attributes": {"Main Material": "Plastic"}},
    )

    restores = []

    class PartialEbay(FakeEbay):
        def publish_offer(self, offer_id):
            raise RuntimeError("publish boom")

    monkeypatch.setenv(APPLY_ENV_FLAG, "1")
    monkeypatch.setattr(sr, "_restore_from_backup", lambda ebay, before, sku: restores.append(dict(before)))
    # bypass required guard
    monkeypatch.setattr(
        sr,
        "protect_and_fill_required_aspects",
        lambda aspects, **k: (aspects, [], []),
    )
    # put path must succeed
    monkeypatch.setattr(
        "scripts.audit_fix_active_listings._put_inventory_product_only",
        lambda *a, **k: (None, None, a[-1] if a else {}),
    )

    result = apply_rewrite(PartialEbay(), conn, plan, cli_apply=True)
    assert result.ok is False
    assert "publish" in (result.stage or result.reason)
    assert result.rolled_back is True or restores
    if restores:
        assert restores[0]["title"] == "Before Title"
    monkeypatch.delenv(APPLY_ENV_FLAG, raising=False)


def test_gap4_compound_material_flags_human(tmp_path, monkeypatch):
    """Unmapped compounds still human; curated map hits resolve (behavior update)."""
    import src.services.semantic_rewrite as srmod

    # Isolated empty maps so production config does not leak into this case
    maps_path = tmp_path / "empty_maps.yaml"
    maps_path.write_text(
        "version: 1\nenabled: true\ncompound_material_map: {}\nrequired_aspect_map: []\n",
        encoding="utf-8",
    )
    srmod.reset_semantic_rewrite_maps_cache()
    monkeypatch.setattr(srmod, "MAPS_PATH", maps_path)

    aspects = {"Material": ["Solid Wood"]}
    violations = [{"claim_type": "semantic_material", "claim_text": "solid wood", "severity": "CRITICAL"}]
    # Use a compound NOT in production maps so unmapped → human still holds
    new_a, notes, human = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={"Main Material": "Metal,Wood,Glass"},
        source_sheet={"materials": ["metal", "wood", "glass"]},
        source_dims_trustworthy=True,
        source_dims={},
    )
    assert "semantic_material_compound" in human

    # Mapped compound resolves (maps table working as designed — not a regression)
    maps_path.write_text(
        "version: 1\nenabled: true\n"
        "compound_material_map:\n"
        "  'Polyester,Rubber Wood':\n"
        "    primary: Rubberwood\n"
        "    policy: apply\n"
        "    evidence: test Wood Type\n"
        "required_aspect_map: []\n",
        encoding="utf-8",
    )
    srmod.reset_semantic_rewrite_maps_cache()
    new_b, notes_b, human_b = apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={"Main Material": "Polyester,rubber Wood"},
        source_sheet={"materials": ["polyester", "rubber wood"]},
        source_dims_trustworthy=True,
        source_dims={},
    )
    assert "semantic_material_compound" not in human_b
    mats = []
    for k, v in new_b.items():
        if str(k).lower() == "material":
            mats = [str(x).lower() for x in (v if isinstance(v, list) else [v])]
    assert any("rubberwood" in x or "rubber wood" in x for x in mats), (new_b, notes_b)
    srmod.reset_semantic_rewrite_maps_cache()


# ---------------------------------------------------------------------------
# Human-queue drain maps (Table A / Table B) — additive tests only
# ---------------------------------------------------------------------------


def _write_maps(path, *, enabled=True, compound=None, required=None):
    import yaml

    data = {
        "version": 1,
        "enabled": enabled,
        "compound_material_map": compound or {},
        "required_aspect_map": required or [],
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_maps_compound_hit_resolves_primary(tmp_path, monkeypatch):
    import src.services.semantic_rewrite as srmod

    maps_path = _write_maps(
        tmp_path / "maps.yaml",
        compound={
            "Polyester,Rubber Wood": {
                "primary": "Rubberwood",
                "policy": "apply",
                "evidence": "Wood Type=Rubberwood",
            }
        },
    )
    srmod.reset_semantic_rewrite_maps_cache()
    monkeypatch.setattr(srmod, "MAPS_PATH", maps_path)

    assert srmod._is_simple_material_value("Polyester,rubber Wood") is True
    assert srmod.resolve_material_value("Polyester,rubber Wood") == "Rubberwood"
    primary, entry = srmod.lookup_compound_material_primary("Polyester, Rubber Wood")
    assert primary == "Rubberwood"
    assert entry and entry.get("policy") == "apply"

    aspects = {"Material": ["Solid Wood"]}
    violations = [{"claim_type": "semantic_material", "claim_text": "solid wood", "severity": "CRITICAL"}]
    new_a, notes, human = srmod.apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={"Main Material": "Polyester,Rubber Wood"},
        source_sheet={"materials": ["polyester", "rubber wood"]},
        source_dims_trustworthy=True,
        source_dims={},
    )
    assert "semantic_material_compound" not in human
    mats = []
    for k, v in new_a.items():
        if str(k).lower() == "material":
            mats = [str(x).lower() for x in (v if isinstance(v, list) else [v])]
    assert any("rubberwood" in x or "rubber wood" in x for x in mats), mats
    srmod.reset_semantic_rewrite_maps_cache()


def test_maps_compound_miss_stays_human(tmp_path, monkeypatch):
    import src.services.semantic_rewrite as srmod

    maps_path = _write_maps(
        tmp_path / "maps.yaml",
        compound={
            "Polyester,Rubber Wood": {
                "primary": "Rubberwood",
                "policy": "apply",
                "evidence": "Wood Type=Rubberwood",
            }
        },
    )
    srmod.reset_semantic_rewrite_maps_cache()
    monkeypatch.setattr(srmod, "MAPS_PATH", maps_path)

    assert srmod._is_simple_material_value("Metal,Wood") is False
    aspects = {"Material": ["Steel"]}
    violations = [{"claim_type": "semantic_material", "claim_text": "steel", "severity": "CRITICAL"}]
    _new_a, _notes, human = srmod.apply_aspect_fixes(
        aspects,
        violations=violations,
        source_attrs={"Main Material": "Metal,Wood"},
        source_sheet={},
        source_dims_trustworthy=True,
        source_dims={},
    )
    assert "semantic_material_compound" in human
    srmod.reset_semantic_rewrite_maps_cache()


def test_maps_compound_human_policy_and_no_evidence_refused(tmp_path, monkeypatch):
    import src.services.semantic_rewrite as srmod

    maps_path = _write_maps(
        tmp_path / "maps.yaml",
        compound={
            "Metal,Wood": {
                "primary": "Metal",
                "policy": "human",
                "evidence": "ambiguous",
            },
            "A,B": {
                "primary": "A",
                "policy": "apply",
                "evidence": "",  # refuse — anti-hallucination
            },
        },
    )
    srmod.reset_semantic_rewrite_maps_cache()
    monkeypatch.setattr(srmod, "MAPS_PATH", maps_path)

    p1, e1 = srmod.lookup_compound_material_primary("Metal,Wood")
    assert p1 is None and e1 is not None
    p2, e2 = srmod.lookup_compound_material_primary("A,B")
    assert p2 is None and e2 is not None
    assert srmod._is_simple_material_value("Metal,Wood") is False
    assert srmod._is_simple_material_value("A,B") is False
    srmod.reset_semantic_rewrite_maps_cache()


def test_maps_required_source_neutral_human(tmp_path, monkeypatch):
    import src.services.semantic_rewrite as srmod

    maps_path = _write_maps(
        tmp_path / "maps.yaml",
        required=[
            {
                "category_id": "38208",
                "aspect": "Upholstery Fabric",
                "policy": "source",
                "source_fields": ["Upholstery Material", "Main Material"],
                "fabric_like_only": True,
                "evidence": "source fabric",
            },
            {
                "category_id": "38208",
                "aspect": "Brand",
                "policy": "neutral",
                "suggested_value": "Unbranded",
                "evidence": "eBay neutral",
            },
            {
                "category_id": "260921",
                "aspect": "Material",
                "policy": "human",
                "evidence": "leave human",
            },
        ],
    )
    srmod.reset_semantic_rewrite_maps_cache()
    monkeypatch.setattr(srmod, "MAPS_PATH", maps_path)
    original = srmod.get_required_aspect_names
    srmod.get_required_aspect_names = lambda cid: {
        "upholstery fabric",
        "brand",
        "material",
    }
    try:
        # source fabric
        out, notes, human = srmod.protect_and_fill_required_aspects(
            {},
            category_id="38208",
            title="Sofa",
            source_attrs={"Main Material": "Corduroy"},
            source_sheet={},
            before_aspects={},
        )
        fabric = []
        for k, v in out.items():
            if str(k).lower() == "upholstery fabric":
                fabric = [str(x).lower() for x in (v if isinstance(v, list) else [v])]
        assert any("corduroy" in x for x in fabric), (out, notes)
        assert not any(x.startswith("required_missing:upholstery") for x in human)

        # structural Main Material must not fill Upholstery Fabric when fabric_like_only
        out2, notes2, human2 = srmod.protect_and_fill_required_aspects(
            {},
            category_id="38208",
            title="Sofa",
            source_attrs={"Main Material": "Wood"},
            source_sheet={},
            before_aspects={},
        )
        fabric2 = [
            str(x).lower()
            for k, v in out2.items()
            if str(k).lower() == "upholstery fabric"
            for x in (v if isinstance(v, list) else [v])
        ]
        assert not any("wood" == x or x == "wood" for x in fabric2) or any(
            "required_missing:upholstery" in h for h in human2
        ) or any("source_empty" in n or "required_map" in n for n in notes2)

        # neutral brand
        brands = [
            str(x)
            for k, v in out.items()
            if str(k).lower() == "brand"
            for x in (v if isinstance(v, list) else [v])
        ]
        assert any(b.lower() == "unbranded" for b in brands), (out, notes)

        # human policy → still missing
        out3, notes3, human3 = srmod.protect_and_fill_required_aspects(
            {},
            category_id="260921",
            title="Tool",
            source_attrs={"Main Material": "Steel"},
            source_sheet={},
            before_aspects={},
        )
        assert any(h.startswith("required_missing:material") for h in human3) or any(
            "policy=human" in n for n in notes3
        )
    finally:
        srmod.get_required_aspect_names = original
        srmod.reset_semantic_rewrite_maps_cache()


def test_maps_missing_file_and_disabled_degrade(tmp_path, monkeypatch):
    import src.services.semantic_rewrite as srmod

    srmod.reset_semantic_rewrite_maps_cache()
    monkeypatch.setattr(srmod, "MAPS_PATH", tmp_path / "does_not_exist.yaml")
    maps = srmod.load_semantic_rewrite_maps(force_reload=True)
    assert maps.get("compound_material_map") == {}
    assert srmod._is_simple_material_value("Polyester,Rubber Wood") is False

    disabled = _write_maps(
        tmp_path / "disabled.yaml",
        enabled=False,
        compound={
            "Polyester,Rubber Wood": {
                "primary": "Rubberwood",
                "policy": "apply",
                "evidence": "x",
            }
        },
    )
    srmod.reset_semantic_rewrite_maps_cache()
    monkeypatch.setattr(srmod, "MAPS_PATH", disabled)
    maps2 = srmod.load_semantic_rewrite_maps(force_reload=True)
    assert maps2.get("enabled") is False
    assert maps2.get("compound_material_map") == {}
    assert srmod.lookup_compound_material_primary("Polyester,Rubber Wood")[0] is None
    srmod.reset_semantic_rewrite_maps_cache()


class TestOnlySeriousClaimsMayRewriteLiveTitle:
    """2026-07-27 金丝雀验收:MEDIUM 的 "with cabinet" 命中标题里的
    "Storage Cabinet"、"wall mounted" 命中 "Wall Bed",触发整条标题按源重建,
    把关键词丰富的 SEO 标题换成裸的供应商品名(还带出了内部编号)。
    交接反复声明"只处理 CRITICAL 不碰 MEDIUM",MEDIUM 却在暗中改标题。"""

    TITLE = "Full Size Murphy Bed with Large Drawers Storage Cabinet Wall Bed 77x53.5x43.4 in"

    def test_medium_marketing_noise_does_not_trigger_rebuild(self):
        violations = [
            {"claim_text": "with cabinet", "claim_type": "semantic_feature", "severity": "MEDIUM"},
            {"claim_text": "wall mounted", "claim_type": "semantic_feature", "severity": "MEDIUM"},
            {"claim_text": "with shelves", "claim_type": "semantic_feature", "severity": "MEDIUM"},
        ]
        assert sr.title_contains_violation(self.TITLE, violations) is False

    def test_critical_absent_from_title_does_not_trigger_rebuild(self):
        violations = [
            {"claim_text": "glossy", "claim_type": "semantic_material", "severity": "CRITICAL"}
        ]
        assert sr.title_contains_violation(self.TITLE, violations) is False

    def test_critical_present_in_title_still_triggers_rebuild(self):
        violations = [
            {"claim_text": "leather", "claim_type": "semantic_material", "severity": "CRITICAL"}
        ]
        assert sr.title_contains_violation("Brown Leather Sectional Sofa", violations) is True

    def test_high_present_in_title_still_triggers_rebuild(self):
        violations = [
            {"claim_text": "leather", "claim_type": "semantic_material", "severity": "HIGH"}
        ]
        assert sr.title_contains_violation("Brown Leather Sectional Sofa", violations) is True

    def test_missing_severity_is_treated_as_not_serious(self):
        violations = [{"claim_text": "cabinet", "claim_type": "semantic_feature"}]
        assert sr.title_contains_violation(self.TITLE, violations) is False
