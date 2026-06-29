import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
requests_stub = types.SimpleNamespace(
    get=lambda *_args, **_kwargs: None,
    post=lambda *_args, **_kwargs: None,
    Session=object,
    utils=types.SimpleNamespace(quote=lambda value: value),
    exceptions=types.SimpleNamespace(HTTPError=Exception),
)
sys.modules.setdefault(
    "dotenv",
    types.SimpleNamespace(load_dotenv=lambda *_args, **_kwargs: None),
)
sys.modules.setdefault("requests", requests_stub)

AUDIT_SPEC = importlib.util.spec_from_file_location(
    "audit_fix_active_listings",
    ROOT / "scripts" / "audit_fix_active_listings.py",
)
audit_fix_active_listings = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(audit_fix_active_listings)

import pytest

@pytest.fixture(autouse=True)
def mock_dependencies(monkeypatch):
    class FakeCategoryMatcher:
        def _get_category_aspects(self, category_id):
            return [], []
    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: FakeCategoryMatcher())
    if hasattr(audit_fix_active_listings, "_fill_missing_required_aspects"):
        monkeypatch.setattr(audit_fix_active_listings, "_fill_missing_required_aspects", lambda aspects, cat, title: aspects)



def test_live_listing_snapshot_prefers_ebay_title_description_aspects_and_category():
    stored_opt = {
        "title": "Stored title",
        "description": "Stored description",
        "aspects": {"Material": ["PU Leather"]},
        "categoryId": "111",
    }
    inventory_item = {
        "product": {
            "title": "Live Genuine Leather Sofa",
            "description": "Inventory description",
            "aspects": {"Material": ["Genuine Leather"]},
        }
    }
    offer = {
        "listingDescription": "Offer description wins",
        "categoryId": "222",
    }

    snapshot = audit_fix_active_listings.build_live_listing_opt_snapshot(
        stored_opt,
        inventory_item=inventory_item,
        offer=offer,
    )

    assert snapshot["title"] == "Live Genuine Leather Sofa"
    assert snapshot["description"] == "Offer description wins"
    assert snapshot["aspects"] == {"Material": ["Genuine Leather"]}
    assert snapshot["categoryId"] == "222"


def test_audit_parser_supports_scheduled_live_email_mode():
    parser = audit_fix_active_listings.create_parser()

    args = parser.parse_args(
        [
            "--live",
            "--email",
            "--exit-zero-on-issues",
            "--record-clean-state",
            "--ignore-clean-freeze",
            "--limit",
            "5",
            "--sku-file",
            "skus.txt",
        ]
    )

    assert args.live is True
    assert args.email is True
    assert args.exit_zero_on_issues is True
    assert args.record_clean_state is True
    assert args.ignore_clean_freeze is True
    assert args.limit == 5
    assert args.sku_file == "skus.txt"


def test_report_source_marks_live_and_local_modes():
    parser = audit_fix_active_listings.create_parser()

    live_args = parser.parse_args(["--live"])
    local_args = parser.parse_args([])

    assert audit_fix_active_listings.get_report_source(live_args) == "live_ebay"
    assert audit_fix_active_listings.get_report_source(local_args) == "local_db_optimization"


def test_live_audit_payload_keeps_raw_description_artifacts_in_fingerprint():
    clean_payload = audit_fix_active_listings.build_live_audit_payload(
        json.dumps(
            {
                "title": "Chair",
                "description": "<div><h3>KEY FEATURES</h3><ul><li>Seat</li></ul></div>",
                "categoryId": "1",
                "aspects": {"Material": ["Wood"]},
            }
        ),
        listing_id="LISTING-1",
        live_inventory={"product": {"imageUrls": [], "videoIds": []}},
    )
    dirty_payload = audit_fix_active_listings.build_live_audit_payload(
        json.dumps(
            {
                "title": "Chair",
                "description": '<div>\\n<div>"><h3>KEY FEATURES</h3><ul><li>Seat</li></ul></div></div>',
                "categoryId": "1",
                "aspects": {"Material": ["Wood"]},
            }
        ),
        listing_id="LISTING-1",
        live_inventory={"product": {"imageUrls": [], "videoIds": []}},
    )

    assert clean_payload["description"] != dirty_payload["description"]
    assert audit_fix_active_listings.build_audit_fingerprint(clean_payload) != (
        audit_fix_active_listings.build_audit_fingerprint(dirty_payload)
    )


def test_frozen_clean_requires_matching_ruleset_version():
    source_fingerprint = "src-1"
    live_fingerprint = "live-1"
    listing_id = "LISTING-1"

    stale_opt = {
        "_quality_gate": {
            "version": audit_fix_active_listings.QUALITY_GATE_META_VERSION,
            "status": "clean",
            "listing_id": listing_id,
            "source_fingerprint": source_fingerprint,
            "live_fingerprint": live_fingerprint,
        }
    }
    current_opt = {
        "_quality_gate": {
            "version": audit_fix_active_listings.QUALITY_GATE_META_VERSION,
            "ruleset_version": audit_fix_active_listings.QUALITY_GATE_RULESET_VERSION,
            "status": "clean",
            "listing_id": listing_id,
            "source_fingerprint": source_fingerprint,
            "live_fingerprint": live_fingerprint,
        }
    }

    assert (
        audit_fix_active_listings.is_listing_frozen_clean(
            stale_opt,
            source_fingerprint=source_fingerprint,
            live_fingerprint=live_fingerprint,
            listing_id=listing_id,
        )
        is False
    )
    assert (
        audit_fix_active_listings.is_listing_frozen_clean(
            current_opt,
            source_fingerprint=source_fingerprint,
            live_fingerprint=live_fingerprint,
            listing_id=listing_id,
        )
        is True
    )


def test_validate_fix_scope_rejects_full_published_fix_without_live():
    parser = audit_fix_active_listings.create_parser()
    args = parser.parse_args(["--fix"])

    try:
        audit_fix_active_listings.validate_fix_scope(args, parser)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected parser.error/SystemExit for non-live full fix")


def test_validate_fix_scope_allows_targeted_non_live_fix():
    parser = audit_fix_active_listings.create_parser()

    sku_args = parser.parse_args(["--fix", "--sku", "SKU-1"])
    file_args = parser.parse_args(["--fix", "--sku-file", "skus.txt"])

    audit_fix_active_listings.validate_fix_scope(sku_args, parser)
    audit_fix_active_listings.validate_fix_scope(file_args, parser)


def test_validate_fix_scope_rejects_record_clean_state_without_live():
    parser = audit_fix_active_listings.create_parser()
    args = parser.parse_args(["--record-clean-state"])

    try:
        audit_fix_active_listings.validate_fix_scope(args, parser)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected parser.error/SystemExit for --record-clean-state without --live")


def test_validate_fix_scope_rejects_ignore_clean_freeze_without_live():
    parser = audit_fix_active_listings.create_parser()
    args = parser.parse_args(["--ignore-clean-freeze"])

    try:
        audit_fix_active_listings.validate_fix_scope(args, parser)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected parser.error/SystemExit for --ignore-clean-freeze without --live")


def test_fix_mode_audit_email_includes_fix_details_and_report_attachment(tmp_path):
    report = {
        "mode": "fix",
        "total_published": 10,
        "total_with_issues": 2,
        "total_transport_failures": 1,
        "severity_counts": {"CRITICAL": 1, "HIGH": 1, "MEDIUM": 0, "LOW": 0},
        "fixed_count": 1,
        "issues": [
            {
                "sku": "SKU-1",
                "listing_id": "123",
                "title": "Live title",
                "issues": [
                    {
                        "severity": "CRITICAL",
                        "type": "hallucinated_foldable",
                        "detail": "Listing says foldable but source does not support it",
                    }
                ],
                "fixes_applied": [
                    "Inventory product fields updated on eBay",
                    "Local DB updated",
                ],
            },
            {
                "sku": "SKU-2",
                "listing_id": "456",
                "title": "Needs manual review",
                "issues": [
                    {
                        "severity": "HIGH",
                        "type": "incomplete_title",
                        "detail": "Title still truncated on eBay",
                    }
                ],
                "fixes_applied": [],
            },
        ],
        "transport_issues": [
            {
                "sku": "SKU-3",
                "listing_id": "789",
                "title": "Fetch timeout",
                "issues": [
                    {
                        "severity": "HIGH",
                        "type": "live_fetch_failed",
                        "detail": "Failed to fetch live eBay listing data: timeout",
                    }
                ],
                "fixes_applied": [],
            }
        ],
    }
    report_path = tmp_path / "listing_audit_fix.json"
    report_path.write_text("{}", encoding="utf-8")

    with patch("src.utils.email_sender.send_email", return_value=True) as mock_send:
        ok = audit_fix_active_listings._send_audit_email(report, report_path)

    assert ok is True
    args, kwargs = mock_send.call_args
    subject, html_body = args
    assert "已修复 1 条" in subject
    assert "抓取/availability 异常" in subject
    assert "已自动修复明细" in html_body
    assert "Inventory product fields updated on eBay" in html_body
    assert "SKU-2" in html_body
    assert "SKU-3" in html_body
    assert "待人工复核" in html_body
    assert "抓取 / Availability 异常" in html_body
    assert kwargs["attachments"] == [str(report_path)]


def test_persist_listing_audit_state_does_not_touch_updated_at_by_default():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            optimization TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO collected_products (sku, optimization, updated_at) VALUES (?, ?, ?)",
        ("SKU-1", json.dumps({}), "2026-06-22 10:00:00"),
    )
    conn.commit()

    changed = audit_fix_active_listings.persist_listing_audit_state(
        conn,
        "SKU-1",
        state="clean",
        source_fingerprint="src-1",
        live_fingerprint="live-1",
        listing_id="LISTING-1",
    )

    row = conn.execute(
        "SELECT optimization, updated_at FROM collected_products WHERE sku = ?",
        ("SKU-1",),
    ).fetchone()
    stored_opt = json.loads(row["optimization"])

    assert changed is True
    assert row["updated_at"] == "2026-06-22 10:00:00"
    assert stored_opt["_quality_gate"]["ruleset_version"] == audit_fix_active_listings.QUALITY_GATE_RULESET_VERSION


def test_load_skus_from_file_ignores_comments_and_duplicates(tmp_path):
    sku_file = tmp_path / "skus.txt"
    sku_file.write_text("SKU1\n# note\nSKU2\nSKU1\n\nSKU3\n", encoding="utf-8")

    assert audit_fix_active_listings._load_skus_from_file(str(sku_file)) == ["SKU1", "SKU2", "SKU3"]


def test_fix_listing_uses_live_snapshot_as_fix_base(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (
            "SKU-LIVE",
            json.dumps(
                {
                    "title": "Stored clean title",
                    "description": "<div>Stored description</div>",
                    "aspects": {"Material": ["Wood"]},
                }
            ),
        ),
    )

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["sku"] = sku
        captured["title"] = title
        captured["description"] = description
        captured["aspects"] = dict(aspects)
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-1", "listing": {"listingId": "123"}, "status": "PUBLISHED"}]

        def publish_offer(self, offer_id):
            captured["published_offer_id"] = offer_id
            return {"listingId": "123"}

    product_row = {
        "sku": "SKU-LIVE",
        "title": "Source title",
        "description": "<div>Source description</div>",
        "optimization": json.dumps(
            {
                "title": "Stored clean title",
                "description": "<div>Stored description</div>",
                "aspects": {"Material": ["Wood"]},
            }
        ),
        "attributes": "{}",
        "specs": "{}",
        "images": "[]",
        "price": 10,
        "suggested_price": 10,
        "listing_id": "123",
    }
    live_opt_raw = json.dumps(
        {
            "title": "Live title truncated at the end Inc",
            "description": "<div>Live description</div>",
            "aspects": {"Material": ["Wood"], "Assembly Status": ["Yes"]},
        }
    )

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-LIVE",
        product_row,
        {"__remove__Assembly Status": True},
        FakeClient(),
        conn,
        base_opt_raw=live_opt_raw,
    )

    assert any("Removed non-applicable aspect: Assembly Status" in item for item in results)
    assert "Assembly Status" not in captured["aspects"]
    assert captured["published_offer_id"] == "offer-1"

    stored_opt = json.loads(
        conn.execute("SELECT optimization FROM collected_products WHERE sku = ?", ("SKU-LIVE",)).fetchone()[0]
    )
    assert "Assembly Status" not in stored_opt["aspects"]


def test_fix_listing_keeps_full_offer_description_when_inventory_copy_is_truncated(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (
            "SKU-DESC",
            json.dumps(
                {
                    "title": "Storage bed",
                    "description": "<div>Original</div>",
                    "aspects": {"Assembly Required": ["Yes"]},
                    "categoryId": "175758",
                }
            ),
        ),
    )

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["inventory_description"] = description
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    long_description = "<div>" + ("A" * 4300) + "</div>"

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-2", "listing": {"listingId": "456"}, "status": "PUBLISHED"}]

        def update_offer_category(self, offer_id, category_id, price=None, listing_description=None):
            captured["offer_description"] = listing_description
            captured["offer_id"] = offer_id
            captured["category_id"] = category_id
            return True

        def publish_offer(self, offer_id):
            return {"listingId": "456"}

    product_row = {
        "sku": "SKU-DESC",
        "title": "Storage bed",
        "description": "<div>Source description</div>",
        "optimization": json.dumps(
            {
                "title": "Storage bed",
                "description": long_description,
                "aspects": {"Assembly Required": ["Yes"]},
                "categoryId": "175758",
            }
        ),
        "attributes": "{}",
        "specs": "{}",
        "images": "[]",
        "price": 10,
        "suggested_price": 10,
        "listing_id": "456",
    }

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-DESC",
        product_row,
        {"__assembly_desc_update__": "Yes"},
        FakeClient(),
        conn,
    )

    assert any("Description assembly wording updated" in item for item in results)
    assert len(captured["inventory_description"]) <= 4000
    assert "Assembly Required" in captured["offer_description"]
    assert len(captured["offer_description"]) > 4000


def test_replace_specifications_table_preserves_package_and_footer():
    original = """
    <div style="max-width:900px;margin:0 auto">
      <div style="padding:25px">
        <h3>KEY FEATURES</h3>
        <p>Feature copy</p>
      </div>
      <!-- Specifications Table -->
      <div style="padding:25px;background:#fff">
        <h3>SPECIFICATIONS</h3>
        <table><tr><td>OLD SPEC</td></tr></table>
      </div>
      <div style="padding:20px 25px;background:#f8f9fa;border-top:1px solid #e0e0e0">
        <h3>PACKAGE INCLUDES</h3>
        <p>1 x chaise lounge</p>
      </div>
      <div style="text-align:center;padding:20px;background:linear-gradient(135deg,#0d1b2a 0%,#1a365d 100%)">
        <p>✦ Ships from California, USA ✦</p>
      </div>
    </div>
    """
    rebuilt = '<!-- Specifications Table --><div style="padding:25px;background:#fff"><h3>SPECIFICATIONS</h3><table><tr><td>NEW SPEC</td></tr></table></div>'

    updated = audit_fix_active_listings.replace_specifications_table_html(original, rebuilt)

    assert "NEW SPEC" in updated
    assert "OLD SPEC" not in updated
    assert "PACKAGE INCLUDES" in updated
    assert "Ships from California, USA" in updated


def test_active_audit_flags_live_description_missing_key_features(monkeypatch):
    class _Matcher:
        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())

    live_opt = {
        "categoryId": "54235",
        "categoryName": "Chairs",
        "title": "Accent Chair",
        "description": "<div><h3>SPECIFICATIONS</h3><table><tr><td>Material</td><td>Wood</td></tr></table></div>",
        "aspects": {
            "Material": ["Wood"],
        },
    }

    issues, fixes = audit_fix_active_listings.audit_single_product(
        "SKU-MISSING-KEY",
        "Accent Chair",
        "{}",
        "{}",
        json.dumps(live_opt),
        "<div>Source description</div>",
    )

    issue_types = {issue["type"] for issue in issues}
    assert "description_structure_missing_key_features" in issue_types
    assert fixes["__restore_live_description_from_local__"] is True


def test_active_audit_flags_live_description_html_artifacts(monkeypatch):
    class _Matcher:
        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())

    live_opt = {
        "categoryId": "54235",
        "categoryName": "Chairs",
        "title": "Accent Chair",
        "description": (
            '<div style="max-width:900px">\\n'
            '<div><p>PREMIUM HOME FURNISHINGS</p>"></div>'
            '<div>"><h3>KEY FEATURES</h3><ul><li>Supportive seat.</li></ul></div>'
            '</div>'
        ),
        "aspects": {"Material": ["Wood"]},
    }

    issues, fixes = audit_fix_active_listings.audit_single_product(
        "SKU-HTML-ARTIFACT",
        "Accent Chair",
        "{}",
        "{}",
        json.dumps(live_opt),
        "<div>Source description</div>",
    )

    issue_types = {issue["type"] for issue in issues}
    assert "description_html_artifacts" in issue_types
    assert fixes["__sanitize_live_description_html__"] is True


def test_active_audit_detects_unsupported_material_cushion_weather_and_position_claims(monkeypatch):
    class _Matcher:
        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())

    source_description = """
    <div>
      产品名称: NADINE CHAISE LOUNGE
      材质: Solid Wood+MDF
      产品特点:
      Featuring adjustable settings and a breathable, slatted design, it ensures maximum comfort.
      Whether you're poolside or on your patio, this chair is perfectly suited for relaxing days spent outdoors.
    </div>
    """
    live_opt = {
        "categoryId": "1255",
        "categoryName": "Patio Chairs",
        "title": "Nadine Outdoor Chaise Lounge Adjustable Acacia Wood with Cushion 78.75",
        "description": (
            "<ul>"
            "<li>Premium Acacia Wood Construction</li>"
            "<li>Five-position backrest lets you seamlessly shift from upright reading posture to full sunbathing relaxation</li>"
            "<li>UV- & Water-Resistant Cushion for all-weather comfort</li>"
            "</ul>"
        ),
        "aspects": {
            "Material": ["Acacia Wood"],
            "Frame Material": ["Acacia Wood"],
            "Upholstery Material": ["Polyester"],
            "Features": ["Adjustable", "With Cushion", "Water Resistant", "UV Resistant"],
        },
    }

    issues, fixes = audit_fix_active_listings.audit_single_product(
        "N767P384822T",
        "NADINE CHAISE LOUNGE",
        json.dumps(
            {
                "Assembled Length (in.)": "78.75",
                "Assembled Width (in.)": "24.00",
                "Assembled Height (in.)": "12.00",
                "Product Weight (lbs.)": "37.48",
            }
        ),
        "{}",
        json.dumps(live_opt),
        source_description,
    )

    issue_types = {issue["type"] for issue in issues}
    assert "hallucinated_wood_species" in issue_types
    assert "hallucinated_cushion" in issue_types
    assert "hallucinated_weather_resistance" in issue_types
    assert "hallucinated_position_count" in issue_types
    assert fixes["__hallucinated_wood_species__"] == "Solid Wood+MDF"


def test_fix_listing_removes_n767_style_semantic_hallucinations(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (
            "N767P384822T",
            json.dumps(
                {
                    "title": "Stored title",
                    "description": "<div>Stored description</div>",
                    "aspects": {"Material": ["Acacia Wood"]},
                }
            ),
        ),
    )

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["sku"] = sku
        captured["title"] = title
        captured["description"] = description
        captured["aspects"] = dict(aspects)
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-1", "listing": {"listingId": "366451031305"}, "status": "PUBLISHED"}]

        def publish_offer(self, offer_id):
            return {"listingId": "366451031305"}

    live_opt_raw = json.dumps(
        {
            "title": "Nadine Outdoor Chaise Lounge Adjustable Acacia Wood with Cushion 78.75",
            "description": (
                "<ul>"
                "<li>Premium Acacia Wood Construction</li>"
                "<li>Five-position backrest lets you seamlessly shift from upright reading posture to full sunbathing relaxation</li>"
                "<li>UV- & Water-Resistant Cushion for all-weather comfort</li>"
                "</ul>"
            ),
            "aspects": {
                "Material": ["Acacia Wood"],
                "Frame Material": ["Acacia Wood"],
                "Upholstery Material": ["Polyester"],
                "Features": ["Adjustable", "With Cushion", "Water Resistant", "UV Resistant"],
            },
        }
    )
    product_row = {
        "sku": "N767P384822T",
        "title": "NADINE CHAISE LOUNGE",
        "description": (
            "<div>产品名称: NADINE CHAISE LOUNGE 材质: Solid Wood+MDF 产品特点 "
            "Prepare to elevate your outdoor setting this summer with our chic wooden chaise lounge. "
            "Featuring adjustable settings and a breathable, slatted design, it ensures maximum comfort.</div>"
        ),
        "optimization": json.dumps({"title": "Stored title", "description": "<div>Stored</div>", "aspects": {}}),
        "attributes": json.dumps(
            {
                "Assembled Length (in.)": "78.75",
                "Assembled Width (in.)": "24.00",
                "Assembled Height (in.)": "12.00",
                "Product Weight (lbs.)": "37.48",
            }
        ),
        "specs": "{}",
        "images": "[]",
        "price": 150,
        "suggested_price": 287.73,
        "listing_id": "366451031305",
    }

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "N767P384822T",
        product_row,
        {
            "__hallucinated_wood_species__": "Solid Wood+MDF",
            "__hallucinated_cushion__": True,
            "__hallucinated_weather_resistance__": True,
            "__hallucinated_position_count__": True,
        },
        FakeClient(),
        conn,
        base_opt_raw=live_opt_raw,
    )

    assert any("unsupported wood species" in item for item in results)
    assert "Acacia" not in captured["title"]
    assert "Cushion" not in captured["title"]
    assert "Acacia" not in captured["description"]
    assert "UV-" not in captured["description"]
    assert "Water-Resistant" not in captured["description"]
    assert "Five-position" not in captured["description"]
    assert "Prepare to elevate your outdoor setting this summer" in captured["description"]
    assert "SPECIFICATIONS" in captured["description"]
    assert captured["aspects"]["Material"] == ["Solid Wood+MDF"]
    assert captured["aspects"]["Frame Material"] == ["Solid Wood+MDF"]
    assert "Upholstery Material" not in captured["aspects"]
    assert captured["aspects"]["Features"] == ["Adjustable"]


def test_fix_listing_restores_live_description_from_local_template(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    local_description = (
        '<div><h3>KEY FEATURES</h3><ul>'
        '<li>Comfortable seating</li>'
        '</ul></div>'
    )
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (
            "SKU-RESTORE-DESC",
            json.dumps(
                {
                    "title": "Stored title",
                    "description": local_description,
                    "aspects": {"Material": ["Wood"]},
                }
            ),
        ),
    )

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["description"] = description
        captured["title"] = title
        captured["aspects"] = dict(aspects)
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-1", "listing": {"listingId": "123"}, "status": "PUBLISHED"}]

        def publish_offer(self, offer_id):
            return {"listingId": "123"}

    product_row = {
        "sku": "SKU-RESTORE-DESC",
        "title": "Source title",
        "description": "<div>Source description</div>",
        "optimization": json.dumps(
            {
                "title": "Stored title",
                "description": local_description,
                "aspects": {"Material": ["Wood"]},
            }
        ),
        "attributes": "{}",
        "specs": "{}",
        "images": "[]",
        "price": 10,
        "suggested_price": 10,
        "listing_id": "123",
    }
    live_opt_raw = json.dumps(
        {
            "title": "Stored title",
            "description": "<div><h3>SPECIFICATIONS</h3><table><tr><td>Material</td><td>Wood</td></tr></table></div>",
            "aspects": {"Material": ["Wood"]},
        }
    )

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-RESTORE-DESC",
        product_row,
        {"__restore_live_description_from_local__": True},
        FakeClient(),
        conn,
        base_opt_raw=live_opt_raw,
    )

    assert any("restored from local KEY FEATURES template" in item for item in results)
    assert "KEY FEATURES" in captured["description"]
    assert "<li" in captured["description"].lower()


def test_fix_listing_sanitizes_local_template_artifacts_before_restore(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    dirty_description = (
        '<div style="max-width:900px">\\n'
        '<div><p>PREMIUM HOME FURNISHINGS</p>"></div>'
        '<div>"><h3>KEY FEATURES</h3><ul><li>Supportive seat.</li></ul></div>'
        '</div>'
    )
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (
            "SKU-RESTORE-DIRTY",
            json.dumps(
                {
                    "title": "Stored title",
                    "description": dirty_description,
                    "aspects": {"Material": ["Wood"]},
                }
            ),
        ),
    )

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["description"] = description
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-1", "listing": {"listingId": "123"}, "status": "PUBLISHED"}]

        def publish_offer(self, offer_id):
            return {"listingId": "123"}

    product_row = {
        "sku": "SKU-RESTORE-DIRTY",
        "title": "Source title",
        "description": "<div>Source description</div>",
        "optimization": json.dumps(
            {
                "title": "Stored title",
                "description": dirty_description,
                "aspects": {"Material": ["Wood"]},
            }
        ),
        "attributes": "{}",
        "specs": "{}",
        "images": "[]",
        "price": 10,
        "suggested_price": 10,
        "listing_id": "123",
    }

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-RESTORE-DIRTY",
        product_row,
        {"__restore_live_description_from_local__": True},
        FakeClient(),
        conn,
    )

    assert any("restored from local KEY FEATURES template" in item for item in results)
    assert "\\n" not in captured["description"]
    assert '"></div>' not in captured["description"]
    assert '>"><h3>' not in captured["description"]


def test_fix_listing_rebuilds_description_from_source_when_local_template_missing(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    conn.execute("INSERT INTO collected_products (sku, optimization) VALUES (?, ?)", ("SKU-REBUILD-DESC", "{}"))

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["description"] = description
        captured["aspects"] = dict(aspects)
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-1", "listing": {"listingId": "123"}, "status": "PUBLISHED"}]

        def update_offer_category(self, offer_id, category_id, price=None, listing_description=None):
            captured["offer_description"] = listing_description
            return True

        def publish_offer(self, offer_id):
            return {"listingId": "123"}

    product_row = {
        "sku": "SKU-REBUILD-DESC",
        "title": "Mid Century Modern Armchair with Frame and Detachable Lumbar Pillow",
        "description": (
            "<h3>Product Features</h3><ul>"
            "<li>Robust wood frame provides lasting everyday support.</li>"
            "<li>Multi-scenario application at home for living rooms, bedrooms, and offices.</li>"
            "<li>Detachable lumbar pillow helps promote comfortable seated posture.</li>"
            "<li>User-friendly assembly with clear instructions and minimal parts.</li>"
            "</ul>"
        ),
        "optimization": "{}",
        "attributes": json.dumps(
            {
                "Assembled Length (in.)": "30.7",
                "Assembled Width (in.)": "23.2",
                "Assembled Height (in.)": "13.7",
                "Product Weight (lbs.)": "28.66",
            }
        ),
        "specs": "{}",
        "images": "[]",
        "price": 10,
        "suggested_price": 10,
        "listing_id": "123",
    }
    base_opt_raw = json.dumps(
        {
            "title": "Mid Century Modern Armchair with Frame and Detachable Lumbar Pillow",
            "description": '<div><h3>SPECIFICATIONS</h3><table><tr><td>Material</td><td>Wood</td></tr></table></div>',
            "categoryId": "38208",
            "aspects": {
                "Type": ["Accent Chair"],
                "Room": ["Living Room"],
                "Assembly Required": ["Yes"],
                "Material": ["Wood"],
            },
        }
    )

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-REBUILD-DESC",
        product_row,
        {"__restore_live_description_from_local__": True},
        FakeClient(),
        conn,
        base_opt_raw=base_opt_raw,
    )

    assert any("rebuilt from source product features" in item for item in results)
    assert "KEY FEATURES" in captured["description"]
    assert "<li" in captured["description"].lower()
    assert "PACKAGE INCLUDES" in captured["description"]
    assert "Detachable Lumbar Pillow" in captured["description"]


def test_fix_listing_marks_local_row_published_after_successful_republish(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE collected_products (
            sku TEXT PRIMARY KEY,
            optimization TEXT,
            status TEXT,
            listing_id TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO collected_products (sku, optimization, status, listing_id, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            "SKU-LOCAL-STATE",
            json.dumps({"title": "Stored title", "description": "<div>Stored description</div>", "aspects": {}}),
            "ERROR",
            "old-listing",
            "2026-06-20 00:00:00",
        ),
    )

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-1", "listing": {"listingId": "old-listing"}, "status": "PUBLISHED"}]

        def update_offer_category(self, offer_id, category_id, price=None, listing_description=None):
            return True

        def publish_offer(self, offer_id):
            return {"listingId": "new-listing-123"}

    product_row = {
        "sku": "SKU-LOCAL-STATE",
        "title": "Source title",
        "description": (
            "<h3>Product Features</h3><ul>"
            "<li>Robust wood frame provides lasting everyday support.</li>"
            "<li>Compact footprint works well in living rooms and bedrooms.</li>"
            "<li>User-friendly assembly with clear instructions and minimal parts.</li>"
            "</ul>"
        ),
        "optimization": "{}",
        "attributes": json.dumps(
            {
                "Assembled Length (in.)": "30.7",
                "Assembled Width (in.)": "23.2",
                "Assembled Height (in.)": "13.7",
            }
        ),
        "specs": "{}",
        "images": "[]",
        "price": 10,
        "suggested_price": 10,
        "listing_id": "old-listing",
    }
    base_opt_raw = json.dumps(
        {
            "title": "Stored title",
            "description": "<div><h3>SPECIFICATIONS</h3><table><tr><td>Material</td><td>Wood</td></tr></table></div>",
            "categoryId": "38208",
            "aspects": {
                "Type": ["Accent Chair"],
                "Room": ["Living Room"],
                "Assembly Required": ["Yes"],
                "Material": ["Wood"],
            },
        }
    )

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-LOCAL-STATE",
        product_row,
        {"__restore_live_description_from_local__": True},
        FakeClient(),
        conn,
        base_opt_raw=base_opt_raw,
    )

    row = conn.execute(
        "SELECT status, listing_id, updated_at FROM collected_products WHERE sku = ?",
        ("SKU-LOCAL-STATE",),
    ).fetchone()

    assert any("republished to live listing (new-listing-123)" in item for item in results)
    assert row[0] == "PUBLISHED"
    assert row[1] == "new-listing-123"
    assert row[2] != "2026-06-20 00:00:00"


def test_fix_listing_removes_claim_diff_pattern_variants(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (
            "SKU-CLAIM-VARIANTS",
            json.dumps(
                {
                    "title": "Stored title",
                    "description": "<div>Stored description</div>",
                    "aspects": {},
                }
            ),
        ),
    )

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["title"] = title
        captured["description"] = description
        captured["aspects"] = dict(aspects)
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-claim", "listing": {"listingId": "123"}, "status": "PUBLISHED"}]

        def publish_offer(self, offer_id):
            return {"listingId": "123"}

    live_opt_raw = json.dumps(
        {
            "title": "Exercise Bike with Rubber Wood Finish",
            "description": (
                "<div>Large backlit display with 8-level resistance and rotating tray "
                "plus durable rubber wood frame.</div>"
            ),
            "aspects": {
                "Features": ["Backlit display", "Rotating tray"],
                "Seating Capacity": ["2 seat"],
                "Material": ["Rubber Wood"],
            },
        }
    )
    product_row = {
        "sku": "SKU-CLAIM-VARIANTS",
        "title": "Exercise Bike",
        "description": "<div>Source description</div>",
        "optimization": json.dumps({"title": "Stored title", "description": "<div>Stored</div>", "aspects": {}}),
        "attributes": "{}",
        "specs": "{}",
        "images": "[]",
        "price": 100,
        "suggested_price": 100,
        "listing_id": "123",
    }

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-CLAIM-VARIANTS",
        product_row,
        {"__claim_diff_violations__": ["led_lighting", "swivel", "8-position", "2-seat", "Rubberwood"]},
        FakeClient(),
        conn,
        base_opt_raw=live_opt_raw,
    )

    cleaned_description = captured["description"].lower()
    cleaned_title = captured["title"].lower()

    assert any("claim violation" in item.lower() for item in results)
    assert "backlit" not in cleaned_description
    assert "rotating" not in cleaned_description
    assert "8-level" not in cleaned_description
    assert "rubber wood" not in cleaned_description
    assert "rubber wood" not in cleaned_title
    assert "Seating Capacity" not in captured["aspects"]
    assert "Material" not in captured["aspects"]


def test_fix_listing_removes_claim_diff_count_mismatch_phrase(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (
            "SKU-COUNT-MISMATCH",
            json.dumps({"title": "Stored title", "description": "<div>Stored description</div>", "aspects": {}}),
        ),
    )

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["title"] = title
        captured["description"] = description
        captured["aspects"] = dict(aspects)
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-count", "listing": {"listingId": "123"}, "status": "PUBLISHED"}]

        def publish_offer(self, offer_id):
            return {"listingId": "123"}

    live_opt_raw = json.dumps(
        {
            "title": "Modern 4-Seat Chenille Sofa",
            "description": "<div>Comfortable 4-seat sofa for daily use.</div>",
            "aspects": {"Seating Capacity": ["4 seats"]},
        }
    )
    product_row = {
        "sku": "SKU-COUNT-MISMATCH",
        "title": "Modern Sofa",
        "description": "<div>Source description</div>",
        "optimization": json.dumps({"title": "Stored title", "description": "<div>Stored</div>", "aspects": {}}),
        "attributes": "{}",
        "specs": "{}",
        "images": "[]",
        "price": 100,
        "suggested_price": 100,
        "listing_id": "123",
    }

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-COUNT-MISMATCH",
        product_row,
        {"__claim_diff_violations__": ["claimed 4-seat, source has 3"]},
        FakeClient(),
        conn,
        base_opt_raw=live_opt_raw,
    )

    assert any("claim violation" in item.lower() for item in results)
    assert "4-seat" not in captured["title"].lower()
    assert "4-seat" not in captured["description"].lower()
    assert "Seating Capacity" not in captured["aspects"]


def test_fix_listing_removes_claim_diff_feature_keys_for_waterproof_and_foldable(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE collected_products (sku TEXT PRIMARY KEY, optimization TEXT)")
    conn.execute(
        "INSERT INTO collected_products (sku, optimization) VALUES (?, ?)",
        (
            "SKU-FEATURE-KEYS",
            json.dumps({"title": "Stored title", "description": "<div>Stored description</div>", "aspects": {}}),
        ),
    )

    captured = {}

    def fake_put_inventory_product_only(ebay_client, sku, title, description, aspects):
        captured["aspects"] = dict(aspects)
        captured["title"] = title
        captured["description"] = description
        return object(), description, aspects

    monkeypatch.setattr(
        audit_fix_active_listings,
        "_put_inventory_product_only",
        fake_put_inventory_product_only,
    )

    class FakeClient:
        def get_offers_by_sku(self, sku):
            return [{"offerId": "offer-feature", "listing": {"listingId": "123"}, "status": "PUBLISHED"}]

        def publish_offer(self, offer_id):
            return {"listingId": "123"}

    live_opt_raw = json.dumps(
        {
            "title": "Canvas Tent",
            "description": "<div>Foldable design with waterproof coverage.</div>",
            "aspects": {
                "Folding Mechanism": ["No"],
                "Is Waterproof": ["Yes"],
                "Water Resistance Technology": ["0-5"],
            },
        }
    )
    product_row = {
        "sku": "SKU-FEATURE-KEYS",
        "title": "Canvas Tent",
        "description": "<div>Source description</div>",
        "optimization": json.dumps({"title": "Stored title", "description": "<div>Stored</div>", "aspects": {}}),
        "attributes": "{}",
        "specs": "{}",
        "images": "[]",
        "price": 100,
        "suggested_price": 100,
        "listing_id": "123",
    }

    results = audit_fix_active_listings.fix_listing_on_ebay(
        "SKU-FEATURE-KEYS",
        product_row,
        {"__claim_diff_violations__": ["foldable", "waterproof"]},
        FakeClient(),
        conn,
        base_opt_raw=live_opt_raw,
    )

    assert any("Aspect Folding Mechanism removed" in item for item in results)
    assert any("Aspect Is Waterproof removed" in item for item in results)
    assert any("Aspect Water Resistance Technology removed" in item for item in results)
    assert "Folding Mechanism" not in captured["aspects"]
    assert "Is Waterproof" not in captured["aspects"]
    assert "Water Resistance Technology" not in captured["aspects"]
