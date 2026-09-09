import json
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AUDIT_SPEC = importlib.util.spec_from_file_location(
    "audit_fix_active_listings",
    ROOT / "scripts" / "audit_fix_active_listings.py",
)
audit_fix_active_listings = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(audit_fix_active_listings)

from src.clients.real_ebay_client import RealEbayClient, normalize_eps_image_data


class _DummyOAuth:
    api_base = "https://api.ebay.com"

    def get_valid_token(self):
        return "token"


class _DummyPolicyManager:
    pass


class _DummyResponse:
    def __init__(self, status_code=204, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class _DummySession:
    def __init__(self):
        self.put_calls = []

    def put(self, url, headers=None, json=None):
        self.put_calls.append(
            {
                "url": url,
                "headers": headers or {},
                "json": json or {},
            }
        )
        return _DummyResponse(204)


def _make_client():
    client = RealEbayClient(_DummyOAuth(), _DummyPolicyManager())
    client.session = _DummySession()
    return client


def test_eps_image_normalization_compresses_large_images():
    from io import BytesIO
    from PIL import Image

    image = Image.effect_noise((1200, 1200), 100).convert("RGB")
    buf = BytesIO()
    image.save(buf, format="PNG")

    data, content_type, size = normalize_eps_image_data(
        buf.getvalue(),
        "image/png",
        max_bytes=1_500_000,
    )

    assert content_type == "image/jpeg"
    assert len(data) <= 1_500_000
    assert size == (1200, 1200)


def test_eps_image_normalization_upscales_small_side():
    from io import BytesIO
    from PIL import Image

    image = Image.new("RGB", (319, 541), "white")
    buf = BytesIO()
    image.save(buf, format="JPEG")

    data, content_type, size = normalize_eps_image_data(buf.getvalue(), "image/jpeg")

    assert content_type == "image/jpeg"
    assert data
    assert size[0] >= 500
    assert size[1] >= 500


def test_eps_image_normalization_downscales_oversized_side():
    from io import BytesIO
    from PIL import Image

    image = Image.new("RGB", (20000, 800), "red")
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95)

    data, content_type, size = normalize_eps_image_data(buf.getvalue(), "image/jpeg")

    assert content_type == "image/jpeg"
    assert data
    assert max(size) <= 14000
    assert sum(size) <= 14900


def test_eps_image_normalization_downscales_oversized_combined_dimension():
    from io import BytesIO
    from PIL import Image

    # Square Giga assets can be under 15000 per side but still fail EPS
    # when width + height exceeds 15000 (error 21916604).
    image = Image.new("RGB", (8335, 8335), "red")
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=90)

    data, content_type, size = normalize_eps_image_data(buf.getvalue(), "image/jpeg")

    assert content_type == "image/jpeg"
    assert data
    assert max(size) <= 14000
    assert sum(size) <= 14900


def test_inventory_item_converts_supplier_urls_to_eps(monkeypatch):
    client = _make_client()
    raw_urls = [
        "https://b2bfiles1.gigab2b.cn/image/a.jpg?x-cc=10&x-cu=106133&x-ct=1773558000&x-cs=abc123&x-oss-process=image/resize,w_800,h_800,m_pad",
        "https://b2bfiles1.gigab2b.cn/image/b.jpg?x-cc=10&x-cu=106133&x-ct=1773558000&x-cs=def456",
    ]
    eps_urls = [
        "https://i.ebayimg.com/images/g/AAA/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/BBB/s-l1600.jpg",
    ]
    upload_calls = []
    get_calls = {"count": 0}

    def fake_upload(image_urls, max_images=24):
        upload_calls.append(list(image_urls))
        return list(eps_urls)

    def fake_get_inventory_item(sku):
        get_calls["count"] += 1
        if get_calls["count"] == 1:
            return {"product": {"imageUrls": []}}
        return {"product": {"imageUrls": list(eps_urls)}}

    monkeypatch.setattr(client, "upload_images_to_eps", fake_upload)
    monkeypatch.setattr(client, "get_inventory_item", fake_get_inventory_item)

    client.create_or_replace_inventory_item(
        "SKU-RAW",
        {
            "title": "Test product",
            "description": "<div>desc</div>",
            "image_urls": list(raw_urls),
            "quantity": 1,
            "condition": "NEW",
            "aspects": {"Brand": ["AquaVerve"]},
        },
    )

    assert upload_calls, "raw supplier URLs should be converted to EPS before PUT"
    assert upload_calls[0][0].startswith("https://b2bfiles1.gigab2b.cn/image/a.jpg?")
    payload = client.session.put_calls[0]["json"]
    assert payload["product"]["imageUrls"] == eps_urls


def test_inventory_item_preserves_existing_ebay_hosted_urls(monkeypatch):
    client = _make_client()
    ebay_urls = [
        "https://i.ebayimg.com/images/g/AAA/s-l1600.jpg",
        "https://i.ebayimg.com/images/g/BBB/s-l1600.jpg",
    ]

    def fail_upload(*args, **kwargs):
        raise AssertionError("existing eBay-hosted URLs should not be re-uploaded")

    monkeypatch.setattr(client, "upload_images_to_eps", fail_upload)
    monkeypatch.setattr(
        client,
        "get_inventory_item",
        lambda sku: {"product": {"imageUrls": list(ebay_urls)}},
    )

    client.create_or_replace_inventory_item(
        "SKU-EPS",
        {
            "title": "Test product",
            "description": "<div>desc</div>",
            "image_urls": list(ebay_urls),
            "quantity": 1,
            "condition": "NEW",
            "aspects": {"Brand": ["AquaVerve"]},
        },
    )

    payload = client.session.put_calls[0]["json"]
    assert payload["product"]["imageUrls"] == ebay_urls


def test_audit_detects_single_image_collapse(monkeypatch):
    class _DummyClient:
        def get_inventory_item(self, sku):
            return {
                "product": {
                    "imageUrls": ["https://i.ebayimg.com/images/g/ONLY/s-l1600.jpg"]
                }
            }

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: object())

    issues, fixes = audit_fix_active_listings.audit_single_product(
        "SKU-COLLAPSE",
        "Platform Bed Frame",
        "{}",
        "{}",
        json.dumps({"aspects": {"Brand": ["AquaVerve"]}}),
        "<div>desc</div>",
        ebay_client=_DummyClient(),
        images_raw=json.dumps(
            [
                "https://b2bfiles1.gigab2b.cn/image/1.jpg?x-cc=10&x-cu=1&x-ct=2&x-cs=3",
                "https://b2bfiles1.gigab2b.cn/image/2.jpg?x-cc=10&x-cu=1&x-ct=2&x-cs=4",
                "https://b2bfiles1.gigab2b.cn/image/3.jpg?x-cc=10&x-cu=1&x-ct=2&x-cs=5",
            ]
        ),
    )

    assert any(issue["type"] == "image_collapse" for issue in issues)
    assert fixes["__restore_images__"] is True


def test_active_audit_uses_shared_measurement_validation(monkeypatch):
    class _Matcher:
        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())

    issues, fixes = audit_fix_active_listings.audit_single_product(
        "SKU-MEASURE",
        "Dining Table",
        json.dumps(
            {
                "Assembled Length (in.)": "72",
                "Assembled Width (in.)": "31.5",
                "Assembled Height (in.)": "28",
                "Product Weight (lbs.)": "85",
            }
        ),
        "{}",
        json.dumps(
            {
                "categoryId": "38204",
                "categoryName": "Dining Tables",
                "aspects": {
                    "Item Length": ["See Description"],
                    "Item Width": ["0 in"],
                    "Item Height": ["9999 in"],
                    "Item Weight": ["Refer to Product Images"],
                },
            }
        ),
        "<div>desc</div>",
    )

    assert any(
        issue["type"] == "missing_dimension" and issue["field"] == "Item Length"
        for issue in issues
    )
    assert any(
        issue["type"] == "wrong_dimension" and issue["field"] == "Item Width"
        for issue in issues
    )
    assert any(
        issue["type"] == "wrong_dimension" and issue["field"] == "Item Height"
        for issue in issues
    )
    assert any(issue["type"] == "missing_weight" for issue in issues)
    assert fixes["Item Length"] == ["72.0 in"]
    assert fixes["Item Width"] == ["31.5 in"]
    assert fixes["Item Height"] == ["28.0 in"]
    assert fixes["Item Weight"] == ["85.0 lbs"]


def test_build_claim_cleanup_pattern_handles_space_separated_claims():
    pattern = audit_fix_active_listings.build_claim_cleanup_pattern("memory foam")

    assert pattern
    assert "memory" in pattern
    assert "foam" in pattern

    cleaned = __import__("re").sub(pattern, "", "Premium memory foam core", flags=__import__("re").IGNORECASE)
    assert "memory foam" not in cleaned.lower()


def test_active_audit_surfaces_description_only_fixes(monkeypatch):
    class _Matcher:
        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            return category_id, category_name

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

    monkeypatch.setattr(audit_fix_active_listings, "get_category_matcher", lambda: _Matcher())

    issues, fixes = audit_fix_active_listings.audit_single_product(
        "SKU-DESC",
        "Chicken Coop",
        json.dumps(
            {
                "Assembled Length (in.)": "80",
                "Assembled Width (in.)": "26",
                "Assembled Height (in.)": "44",
                "Product Weight (lbs.)": "52",
            }
        ),
        "{}",
        json.dumps(
            {
                "categoryId": "46289",
                "categoryName": "Chicken Coops",
                "aspects": {
                    "Brand": ["AquaVerve"],
                    "Item Length": ["80 in"],
                    "Item Width": ["26 in"],
                    "Item Height": ["44 in"],
                    "Item Weight": ["52 lbs"],
                },
                "description": "<table><tr><td>Overall Dimensions (L×W×H)</td><td>80.0 × 26.0 × 44.0 inches</td></tr><tr><td>Overall Weight</td><td>Not specified</td></tr></table>",
            }
        ),
        "<div>source desc</div>",
    )

    issue_types = {issue["type"] for issue in issues}
    assert issue_types == {
        "description_structure_missing_key_features",
        "desc_weight_mismatch",
    }
    assert fixes == {
        "__restore_live_description_from_local__": True,
        "__desc_needs_update__": True,
    }
