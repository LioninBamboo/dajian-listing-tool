import importlib.util
import json
import sys
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUDIT_SPEC = importlib.util.spec_from_file_location(
    "audit_fix_active_listings",
    ROOT / "scripts" / "audit_fix_active_listings.py",
)
audit_fix_active_listings = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(audit_fix_active_listings)

import server as server_mod
from src.clients.dajian_client import extract_product_video_urls
from src.utils.listing_quality_gate import build_source_facts


def test_extract_product_video_urls_prefers_product_video_url_and_dedupes():
    detail = {
        "productVideoUrl": "https://cdn.example.com/video/main.mp4?token=1",
        "videoUrls": [
            "https://cdn.example.com/video/main.mp4?token=2",
            "https://cdn.example.com/video/extra.mp4",
            "",
        ],
    }

    assert extract_product_video_urls(detail) == [
        "https://cdn.example.com/video/main.mp4?token=1",
        "https://cdn.example.com/video/extra.mp4",
    ]


def test_collection_enrichment_includes_product_video_url(monkeypatch):
    class _FakeDaJianClient:
        def __init__(self, client_id, client_secret):
            self.client_id = client_id
            self.client_secret = client_secret

        def get_product_detail_by_sku(self, sku):
            assert sku == "SKU-VIDEO"
            return {
                "productName": 'Chicken Coop',
                "description": "",
                "mainImageUrl": "https://example.com/a.jpg",
                "imageUrls": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
                "attributes": {"Color": "Gray"},
                "characteristics": ["Feature 1", "Feature 2"],
                "productVideoUrl": "https://example.com/source.mp4?sig=1",
                "videoUrls": [],
            }

    monkeypatch.setenv("DAJIAN_API_KEY", "key")
    monkeypatch.setenv("DAJIAN_API_SECRET", "secret")
    monkeypatch.setattr("src.clients.dajian_client.DaJianClient", _FakeDaJianClient)

    enrichment = server_mod.fetch_dajian_collection_enrichment("SKU-VIDEO")

    assert enrichment["videos"] == ["https://example.com/source.mp4?sig=1"]
    assert enrichment["images"] == ["https://example.com/a.jpg", "https://example.com/b.jpg"]
    assert enrichment["title"] == "Chicken Coop"


def test_audit_detects_missing_live_video_when_source_has_video(monkeypatch):
    class _Matcher:
        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

    class _DummyClient:
        def get_inventory_item(self, sku):
            return {
                "product": {
                    "imageUrls": ["https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"],
                    "videoIds": [],
                }
            }

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())

    issues, fixes = audit_fix_active_listings.audit_single_product(
        "SKU-VIDEO",
        "Chicken Coop",
        "{}",
        "{}",
        json.dumps({"categoryId": "46289", "categoryName": "Chicken Coops", "aspects": {}}),
        "<div>desc</div>",
        ebay_client=_DummyClient(),
        images_raw=json.dumps(["https://example.com/a.jpg", "https://example.com/b.jpg"]),
        videos_raw=json.dumps(["https://example.com/source.mp4?sig=1"]),
    )

    assert any(issue["type"] == "missing_video" for issue in issues)
    assert fixes["__sync_video__"] == "https://example.com/source.mp4?sig=1"


def test_collection_issue_flags_mark_non_publishable_source_video():
    payload = server_mod.ProductPayload(
        sku="SKU-VIDEO-TXT",
        title="Chicken Coop",
        price=199.0,
        description="<div>Chicken coop with run and nesting box for backyard use.</div>",
        images=["https://example.com/a.jpg", "https://example.com/b.jpg"],
        videos=["https://media.example.com/video_manifest.txt"],
        attributes={"Color": "Gray"},
        specs={"Material": "Fir Wood"},
    )

    source_facts = build_source_facts(
        source_title=payload.title,
        source_description=payload.description,
        attributes=payload.attributes,
        specs=payload.specs,
        videos=payload.videos,
    )
    flags = server_mod._collection_issue_flags(
        payload,
        payload.images,
        payload.videos,
        source_facts=source_facts,
    )

    assert "source_video_not_publishable" in flags


def test_ready_draft_audit_selects_videos_column():
    source = (ROOT / "scripts" / "audit_fix_ready_drafts.py").read_text(encoding="utf-8-sig")
    assert re.search(
        r"SELECT id, sku, title, description, attributes, specs, optimization, logs, images, videos, url, status",
        source,
    )
