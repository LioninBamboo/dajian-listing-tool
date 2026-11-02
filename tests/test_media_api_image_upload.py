"""Media API image upload: mocked HTTP only, never production eBay."""

from __future__ import annotations

import json
import logging
import sys
import time
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import batch_publish
from src.clients.ebay_media_images import (
    CREATE_IMAGE_FROM_FILE,
    CREATE_IMAGE_FROM_URL,
    GET_IMAGE_URL,
    MEDIA_IMAGE_API,
    SELL_INVENTORY_SCOPE,
    MediaApiScopeError,
    MediaImageUploadError,
    format_media_scope_error,
    media_url_prefix,
)
from src.clients.real_ebay_client import RealEbayClient
from src.services.motors_trading import _pictures_xml
from src.utils.store_profile import StoreProfile

SECRET_TOKEN = "SECRET_TOKEN_VALUE_XYZ"
SIGNED_SOURCE = (
    "https://b2bfiles1.gigab2b.cn/image/demo.jpg"
    "?x-cc=AAA&x-cu=BBB&x-ct=CCC&x-cs=DD%2BEE%3D"
)
HOSTED_A = "https://i.ebayimg.com/images/g/AAA/s-l1600.jpg"
HOSTED_B = "https://i.ebayimg.com/images/g/BBB/s-l1600.jpg"
HOSTED_C = "https://i.ebayimg.com/images/g/CCC/s-l1600.jpg"


class DummyOAuth:
    api_base = "https://api.ebay.com"
    environment = "PRODUCTION"

    def __init__(self, token=SECRET_TOKEN):
        self.token = token
        self.refresh_calls = 0

    def get_valid_token(self):
        return self.token

    def refresh_access_token(self):
        self.refresh_calls += 1
        self.token = "REFRESHED_TOKEN_ABC"
        return {"access_token": self.token}


class DummyPolicyManager:
    pass


class DummyPutSession:
    def __init__(self):
        self.put_calls = []

    def put(self, url, headers=None, json=None):
        self.put_calls.append({"url": url, "headers": headers or {}, "json": json or {}})

        class _Resp:
            status_code = 204
            text = ""

            def json(self):
                return {}

        return _Resp()


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None, content=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        if content is not None:
            self.content = content
        elif payload is not None:
            self.content = json.dumps(payload).encode("utf-8")
        else:
            self.content = b""
        if text is not None:
            self.text = text
        else:
            self.text = self.content.decode("utf-8", "replace") if self.content else ""

    def json(self):
        if self._payload is not None:
            return self._payload
        if not self.content:
            raise ValueError("no json")
        return json.loads(self.content)


class ScriptedSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        record = {"method": str(method).upper(), "url": url}
        record.update(kwargs)
        self.calls.append(record)
        if not self.responses:
            raise AssertionError(f"unexpected {method} {url}")
        item = self.responses.pop(0)
        if callable(item):
            return item(method, url, **kwargs)
        return item


def _jpeg_bytes(width=600, height=600):
    buf = BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _hosted_201(url=HOSTED_A, expiration="2030-01-01T00:00:00Z"):
    return FakeResponse(
        201,
        {"imageUrl": url, "expirationDate": expiration},
        headers={"Location": f"{MEDIA_IMAGE_API}/image/img-1"},
    )


def _error(status, error_id, message="boom", headers=None):
    return FakeResponse(
        status,
        {"errors": [{"errorId": error_id, "message": message}]},
        headers=headers or {},
    )


def _make_client(session=None):
    client = RealEbayClient(DummyOAuth(), DummyPolicyManager())
    client.session = DummyPutSession()
    client.media_images.min_post_interval = 0
    if session is not None:
        client.media_images.session = session
    return client


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)


def test_success_url_path_posts_json_and_bearer():
    session = ScriptedSession([_hosted_201()])
    client = _make_client(session)

    result = client.upload_images_to_eps([SIGNED_SOURCE])

    assert result == [HOSTED_A]
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == CREATE_IMAGE_FROM_URL
    assert call["json"] == {"imageUrl": SIGNED_SOURCE}
    assert call["headers"]["Authorization"] == f"Bearer {SECRET_TOKEN}"
    assert call["headers"]["Content-Type"] == "application/json"


def test_success_via_location_and_get_image():
    image_id = "img-location-9"
    session = ScriptedSession(
        [
            FakeResponse(
                201,
                payload=None,
                headers={"Location": f"{MEDIA_IMAGE_API}/image/{image_id}"},
                content=b"",
            ),
            FakeResponse(200, {"imageUrl": HOSTED_B, "expirationDate": "2031-01-01T00:00:00Z"}),
        ]
    )
    client = _make_client(session)

    result = client.upload_images_to_eps(["https://cdn.example/a.jpg"])

    assert result == [HOSTED_B]
    assert session.calls[0]["url"] == CREATE_IMAGE_FROM_URL
    assert session.calls[1]["method"] == "GET"
    assert session.calls[1]["url"] == GET_IMAGE_URL.format(image_id=image_id)
    assert len(session.calls) == 2


def test_scheme_relative_url_gets_https_prefix_only():
    session = ScriptedSession([_hosted_201()])
    client = _make_client(session)
    source = "//b2bfiles1.gigab2b.cn/image/a.jpg?x-cc=AAA"

    assert client.upload_images_to_eps([source]) == [HOSTED_A]
    assert session.calls[0]["json"]["imageUrl"] == "https:" + source


def test_url_params_byte_identical_on_media_and_file_download(monkeypatch):
    downloaded = []

    def fake_get(url, **kwargs):
        downloaded.append(url)
        return FakeResponse(200, headers={"Content-Type": "image/jpeg"}, content=_jpeg_bytes())

    monkeypatch.setattr("requests.get", fake_get)
    session = ScriptedSession(
        [
            _error(400, 190204, "not downloadable"),
            _hosted_201(HOSTED_C),
        ]
    )
    client = _make_client(session)

    result = client.upload_images_to_eps([SIGNED_SOURCE])

    assert result == [HOSTED_C]
    assert session.calls[0]["json"]["imageUrl"] == SIGNED_SOURCE
    assert downloaded == [SIGNED_SOURCE]
    assert session.calls[1]["url"] == CREATE_IMAGE_FROM_FILE
    assert "image" in session.calls[1]["files"]


def test_prepare_inventory_preserves_giga_signature_params():
    session = ScriptedSession([_hosted_201(), _hosted_201(HOSTED_B)])
    client = _make_client(session)
    raw_urls = [
        SIGNED_SOURCE + "&x-oss-process=image/resize,w_800",
        "https://b2bfiles1.gigab2b.cn/image/b.jpg?x-cc=AAA&x-cu=BBB&x-ct=CCC&x-cs=FF%2BGG%3D",
    ]
    client.get_inventory_item = lambda sku: {"product": {"imageUrls": []}}

    prepared = client._prepare_inventory_image_urls("SKU-SIG", raw_urls, max_images=24)

    assert prepared == [HOSTED_A, HOSTED_B]
    sent = [call["json"]["imageUrl"] for call in session.calls]
    for body_url in sent:
        assert "x-cc=AAA" in body_url
        assert "x-cu=BBB" in body_url
        assert "x-ct=CCC" in body_url
        assert "x-cs=" in body_url
        assert "x-oss-process" not in body_url
    assert "DD%2BEE%3D" in sent[0] or "DD+EE=" in sent[0]


def test_retry_503_then_success(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    session = ScriptedSession([_error(503, 190000), _error(503, 190000), _hosted_201()])
    client = _make_client(session)

    assert client.upload_images_to_eps(["https://cdn.example/a.jpg"]) == [HOSTED_A]
    assert [call["url"] for call in session.calls] == [CREATE_IMAGE_FROM_URL] * 3
    assert sleeps[:2] == [1.0, 2.0]


def test_retry_503_x3_fails_without_file_fallback(monkeypatch):
    downloaded = []
    monkeypatch.setattr("requests.get", lambda *a, **k: downloaded.append(a) or FakeResponse(200))
    session = ScriptedSession([_error(503, 190000), _error(503, 190000), _error(503, 190000)])
    client = _make_client(session)

    with pytest.raises(MediaImageUploadError) as excinfo:
        client.upload_images_to_eps(["https://cdn.example/a.jpg"])

    assert excinfo.value.uploaded_count == 0
    assert len(session.calls) == 3
    assert all(call["url"] == CREATE_IMAGE_FROM_URL for call in session.calls)
    assert downloaded == []


def test_retry_after_is_honoured_and_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    session = ScriptedSession(
        [_error(429, 190000, headers={"Retry-After": "45"}), _hosted_201()]
    )
    client = _make_client(session)

    assert client.upload_images_to_eps(["https://cdn.example/a.jpg"]) == [HOSTED_A]
    assert sleeps == [30.0]


def test_400_is_not_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    session = ScriptedSession([_error(400, 199999, "bad request")])
    client = _make_client(session)

    with pytest.raises(MediaImageUploadError):
        client.upload_images_to_eps(["https://cdn.example/a.jpg"])

    assert len(session.calls) == 1
    assert sleeps == []


def test_401_refreshes_once_then_fails():
    session = ScriptedSession([_error(401, 1001, "Invalid access token"), _error(401, 1001, "Invalid access token")])
    client = _make_client(session)

    with pytest.raises(MediaImageUploadError):
        client.upload_images_to_eps(["https://cdn.example/a.jpg"])

    assert client.oauth.refresh_calls == 1
    assert len(session.calls) == 2
    assert session.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET_TOKEN}"
    assert session.calls[1]["headers"]["Authorization"] == "Bearer REFRESHED_TOKEN_ABC"
    assert all(call["url"] == CREATE_IMAGE_FROM_URL for call in session.calls)


def test_fallback_190204_uses_multipart_image_field(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda url, **kwargs: FakeResponse(200, headers={"Content-Type": "image/jpeg"}, content=_jpeg_bytes()),
    )
    session = ScriptedSession([_error(400, 190204), _hosted_201(HOSTED_B)])
    client = _make_client(session)

    assert client.upload_images_to_eps(["https://cdn.example/a.jpg"]) == [HOSTED_B]
    file_call = session.calls[1]
    assert file_call["url"] == CREATE_IMAGE_FROM_FILE
    assert "image" in file_call["files"]
    filename, data, content_type = file_call["files"]["image"]
    assert filename
    assert data
    assert content_type.startswith("image/")


def test_http_source_goes_straight_to_file(monkeypatch):
    downloaded = []

    def fake_get(url, **kwargs):
        downloaded.append(url)
        return FakeResponse(200, headers={"Content-Type": "image/jpeg"}, content=_jpeg_bytes())

    monkeypatch.setattr("requests.get", fake_get)
    session = ScriptedSession([_hosted_201()])
    client = _make_client(session)
    http_url = "http://cdn.example/plain.jpg"

    assert client.upload_images_to_eps([http_url]) == [HOSTED_A]
    assert downloaded == [http_url]
    assert [call["url"] for call in session.calls] == [CREATE_IMAGE_FROM_FILE]


def test_5xx_does_not_fall_back_to_file(monkeypatch):
    downloaded = []
    monkeypatch.setattr("requests.get", lambda *a, **k: downloaded.append("hit") or FakeResponse(200))
    session = ScriptedSession([_error(500, 190000), _error(500, 190000), _error(500, 190000)])
    client = _make_client(session)

    with pytest.raises(MediaImageUploadError):
        client.upload_images_to_eps(["https://cdn.example/a.jpg"])

    assert downloaded == []
    assert len(session.calls) == 3


@pytest.mark.parametrize("error_id", [190013, 1100])
def test_missing_scope_raises_media_api_scope_error(error_id, caplog, monkeypatch):
    monkeypatch.setattr(
        "src.clients.ebay_media_images.get_store_profile",
        lambda: StoreProfile(brand_name="GrovePop"),
    )
    session = ScriptedSession([_error(403, error_id, "Unauthorized access.")])
    client = _make_client(session)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MediaApiScopeError) as excinfo:
            client.upload_images_to_eps(["https://cdn.example/a.jpg"])

    message = str(excinfo.value)
    assert "GrovePop" in message
    assert "sell.inventory" in message
    assert SELL_INVENTORY_SCOPE in message
    assert SECRET_TOKEN not in message
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET_TOKEN not in logged
    assert "Authorization" not in logged


def test_partial_failure_raises_and_returns_no_list():
    session = ScriptedSession(
        [
            _hosted_201(HOSTED_A),
            _error(400, 199999, "permanent"),
        ]
    )
    client = _make_client(session)
    urls = [
        "https://cdn.example/a.jpg",
        "https://cdn.example/b.jpg",
        "https://cdn.example/c.jpg",
    ]

    with pytest.raises(MediaImageUploadError) as excinfo:
        returned = client.upload_images_to_eps(urls)
        assert returned is None  # must not return a partial list

    assert excinfo.value.uploaded_count == 1
    assert excinfo.value.failed[0][0] == 1
    assert media_url_prefix(urls[1]) == excinfo.value.failed[0][1]
    assert len(session.calls) == 2


def test_already_hosted_passthrough_makes_no_http_calls():
    session = ScriptedSession([])
    client = _make_client(session)

    result = client.upload_images_to_eps([HOSTED_A, HOSTED_B])

    assert result == [HOSTED_A, HOSTED_B]
    assert session.calls == []


def test_motors_siteid_100_xml_uses_media_urls_without_siteid_on_media(monkeypatch):
    aqua = StoreProfile(
        brand_name="AquaRides",
        listing_channel="trading",
        ebay_site_id="100",
        category_tree_id="100",
    )
    monkeypatch.setattr("src.utils.store_profile.get_store_profile", lambda: aqua)
    monkeypatch.setattr("src.clients.real_ebay_client.get_store_profile", lambda: aqua)

    hosted = "https://i.ebayimg.com/images/g/AAA/s-l1600.jpg?x=1&y=2"
    session = ScriptedSession([_hosted_201(hosted)])
    client = _make_client(session)
    uploaded = client.upload_images_to_eps(["https://cdn.example/rack.jpg"])
    assert uploaded == [hosted]
    for call in session.calls:
        header_names = {str(name).lower() for name in (call.get("headers") or {})}
        assert "x-ebay-api-siteid" not in header_names

    trading = []

    def fake_post(url, headers=None, data=None, timeout=None):
        trading.append({"url": url, "headers": headers or {}, "data": data})
        return FakeResponse(200, text="<Ack>Success</Ack><ItemID>188000000001</ItemID>")

    client.session.post = fake_post
    result = client.add_fixed_price_item_motors(
        {
            "sku": "MOT-1",
            "title": "Roof Rack",
            "description": "<div>Rack</div>",
            "image_urls": uploaded,
            "aspects": {"Brand": ["AquaRides"]},
        },
        category_id="33651",
        price=99.0,
        quantity=1,
    )
    assert result["itemId"] == "188000000001"
    xml = trading[0]["data"].decode("utf-8")
    assert xml_escape(hosted) in xml
    assert "<PictureURL>" in xml
    assert _pictures_xml(uploaded) in xml
    assert trading[0]["headers"]["X-EBAY-API-SITEID"] == "100"
    assert "&amp;" in xml


def test_logs_never_include_query_string(caplog):
    session = ScriptedSession([_hosted_201()])
    client = _make_client(session)
    with caplog.at_level(logging.DEBUG):
        client.upload_images_to_eps([SIGNED_SOURCE])
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "x-cs=" not in logged
    assert "x-cc=" not in logged
    assert SECRET_TOKEN not in logged


def _stub_publish_pipeline(monkeypatch, client_cls):
    class _FakeOAuth:
        api_base = "https://api.ebay.example"

        def __init__(self, environment):
            self.environment = environment

        def is_authorized(self):
            return True

        def get_application_token(self):
            return "app-token"

        def get_valid_token(self):
            return "token"

    class _FakeCategoryMatcher:
        def __init__(self, oauth):
            pass

        def canonicalize_category(self, title_context, category_id, category_name, description=None):
            return category_id, category_name

        def get_category_and_aspects(self, title, aspects, description):
            return "38204", "Coffee Tables", aspects

        def is_category_plausible_for_text(self, title, category_id, category_name):
            return True

        def _get_category_aspects(self, category_id):
            return [], []

    class _FakePolicyManager:
        def __init__(self, oauth):
            pass

    monkeypatch.setattr(batch_publish, "fetch_market_price", lambda title: None)
    monkeypatch.setattr(batch_publish, "calculate_smart_final_price", lambda product, market_p: 99.0)
    monkeypatch.setattr(batch_publish, "_is_sellable_leaf_category", lambda oauth, category_id: True)
    monkeypatch.setattr(batch_publish, "_try_upload_video", lambda *a, **k: (None, "ok"))
    monkeypatch.setattr(batch_publish, "_persist_video_status", lambda *a, **k: None)
    monkeypatch.setattr(batch_publish, "_persist_prepared_listing", lambda **k: None)
    monkeypatch.setattr(batch_publish, "_sync_motors_compatibility", lambda *a, **k: None)
    monkeypatch.setattr(batch_publish, "update_product_status", lambda *a, **k: None)
    monkeypatch.setattr(batch_publish, "resolve_publish_quantity", lambda sku, logger=None: 1)
    monkeypatch.setattr(
        batch_publish,
        "run_listing_qc",
        lambda **kwargs: {
            "status": "pass",
            "blockers": [],
            "warnings": [],
            "source_fingerprint": "source",
            "candidate_fingerprint": "candidate",
            "ruleset_version": "listing-qc-v2",
            "fact_sheet_version": 4,
        },
    )
    monkeypatch.setattr("src.services.ebay_auth.EbayOAuthService", _FakeOAuth)
    monkeypatch.setattr("src.services.ebay_category_matcher.EbayCategoryMatcher", _FakeCategoryMatcher)
    monkeypatch.setattr("src.services.ebay_policy_manager.EbayPolicyManager", _FakePolicyManager)
    monkeypatch.setattr("src.clients.real_ebay_client.RealEbayClient", client_cls)


def _coffee_table_product():
    return {
        "sku": "SKU-IMG-FAIL",
        "title": "Coffee Table",
        "description": "<div>Assembly Required Yes</div>",
        "price": 50.0,
        "suggested_price": 99.0,
        "shipping": 0,
        "images": [
            "https://cdn.example/a.jpg",
            "https://cdn.example/b.jpg",
            "https://cdn.example/c.jpg",
        ],
        "videos": [],
        "attributes": {
            "Assembled Length (in.)": "40",
            "Assembled Width (in.)": "20",
            "Assembled Height (in.)": "18",
        },
        "specs": {},
        "cost_breakdown": {"total_dajian_cost": 40},
        "optimization": {
            "title": "Coffee Table",
            "description": (
                "<div><h3>KEY FEATURES</h3><ul>"
                "<li>Storage shelf keeps living room essentials organized.</li>"
                "<li>Sturdy frame supports everyday coffee table use.</li>"
                "<li>Compact profile fits apartments and family rooms.</li>"
                "</ul></div>"
            ),
            "categoryId": "38204",
            "categoryName": "Coffee Tables",
            "aspects": {
                "Brand": ["AquaVerve"],
                "Type": ["Coffee Table"],
                "Assembly Required": ["Yes"],
                "Item Length": ["40 in"],
                "Item Width": ["20 in"],
                "Item Height": ["18 in"],
            },
        },
    }


def test_batch_publish_partial_image_failure_blocks_sku_and_writes(monkeypatch):
    calls = {"inventory": 0, "offer": 0, "trading": 0}

    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            pass

        def _prepare_inventory_image_urls(self, sku, raw_images, max_images=24):
            raise MediaImageUploadError(
                failed=[(1, "https://cdn.example/b.jpg", "HTTP 400 errorId=199999")],
                uploaded_count=1,
            )

        def create_or_replace_inventory_item(self, *args, **kwargs):
            calls["inventory"] += 1
            raise AssertionError("inventory PUT must not run after image failure")

        def create_offer(self, *args, **kwargs):
            calls["offer"] += 1
            raise AssertionError("offer must not run after image failure")

        def add_fixed_price_item_motors(self, *args, **kwargs):
            calls["trading"] += 1
            raise AssertionError("AddFixedPriceItem must not run after image failure")

    recorded = []
    _stub_publish_pipeline(monkeypatch, _FakeEbayClient)
    monkeypatch.setattr(batch_publish, "record_error", lambda sku, msg: recorded.append((sku, msg)))

    result = batch_publish.publish_single_product(_coffee_table_product())

    assert result["status"] == "error"
    assert result["message"].startswith("image upload failed: 1 of 3 images:")
    assert calls == {"inventory": 0, "offer": 0, "trading": 0}
    assert recorded and recorded[0][0] == "SKU-IMG-FAIL"


def test_batch_publish_exit_code_nonzero_on_image_error(monkeypatch, tmp_path):
    monkeypatch.setattr(batch_publish, "LOG_DIR", tmp_path)
    monkeypatch.setattr(
        batch_publish,
        "get_ready_products",
        lambda *a, **k: [{"sku": "S1", "title": "Table", "suggested_price": 10}],
    )
    monkeypatch.setattr(
        batch_publish,
        "publish_single_product",
        lambda *a, **k: {"status": "error", "message": "image upload failed: 1 of 3 images: boom"},
    )
    monkeypatch.setattr(sys, "argv", ["batch_publish.py"])
    assert batch_publish.main() == 1


def test_batch_publish_exit_code_nonzero_on_scope_error(monkeypatch, tmp_path):
    seen = []

    def fake_publish(product, dry_run=False):
        seen.append(product["sku"])
        raise MediaApiScopeError(format_media_scope_error(403, 190013))

    monkeypatch.setattr(batch_publish, "LOG_DIR", tmp_path)
    monkeypatch.setattr(
        batch_publish,
        "get_ready_products",
        lambda *a, **k: [
            {"sku": "S1", "title": "A", "suggested_price": 10},
            {"sku": "S2", "title": "B", "suggested_price": 10},
        ],
    )
    monkeypatch.setattr(batch_publish, "publish_single_product", fake_publish)
    monkeypatch.setattr(
        "src.clients.ebay_media_images.get_store_profile",
        lambda: StoreProfile(brand_name="GrovePop"),
    )
    monkeypatch.setattr(sys, "argv", ["batch_publish.py"])
    assert batch_publish.main() == 1
    assert seen == ["S1"]


def test_dry_run_does_not_construct_ebay_client(monkeypatch):
    constructed = {"n": 0}

    class _FakeEbayClient:
        def __init__(self, oauth, policy_manager):
            constructed["n"] += 1
            raise AssertionError("dry-run must not construct RealEbayClient")

    _stub_publish_pipeline(monkeypatch, _FakeEbayClient)
    result = batch_publish.publish_single_product(_coffee_table_product(), dry_run=True)
    assert result["status"] == "dry_run"
    assert constructed["n"] == 0


def test_prepare_keeps_live_hosted_set_when_upload_fails_for_existing_sku():
    session = ScriptedSession([_error(400, 199999)])
    client = _make_client(session)
    live = [HOSTED_A, HOSTED_B]
    client.get_inventory_item = lambda sku: {"product": {"imageUrls": list(live)}}

    prepared = client._prepare_inventory_image_urls(
        "SKU-LIVE",
        ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        max_images=24,
    )
    assert prepared == live


def test_prepare_raises_when_new_sku_upload_fails():
    session = ScriptedSession([_error(400, 199999)])
    client = _make_client(session)
    client.get_inventory_item = lambda sku: None

    with pytest.raises(MediaImageUploadError):
        client._prepare_inventory_image_urls(
            "SKU-NEW",
            ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
            max_images=24,
        )


def test_trading_uploadsitehostedpictures_removed_from_client():
    source = (ROOT / "src" / "clients" / "real_ebay_client.py").read_text(encoding="utf-8")
    assert 'X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures"' not in source
    assert "UploadSiteHostedPicturesRequest" not in source
    media_src = (ROOT / "src" / "clients" / "ebay_media_images.py").read_text(encoding="utf-8")
    assert "create_image_from_url" in media_src


def test_scope_error_message_omits_token():
    text = format_media_scope_error(403, 190013)
    assert SECRET_TOKEN not in text
    assert "sell.inventory" in text
