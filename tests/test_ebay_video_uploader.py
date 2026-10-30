from src.services.ebay_video_uploader import EbayVideoUploader


class _DummyOAuth:
    environment = "PRODUCTION"

    def get_valid_token(self):
        return "test-token"


class _DummyResponse:
    status_code = 200
    text = ""


def test_upload_video_content_sends_full_payload(monkeypatch):
    uploader = EbayVideoUploader(_DummyOAuth())
    payload = b"a" * (6 * 1024 * 1024)
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["data"] = kwargs["data"]
        return _DummyResponse()

    monkeypatch.setattr(uploader, "_request_with_retry", fake_request)

    assert uploader.upload_video_content("video123", payload) is True
    assert captured["method"] == "POST"
    assert captured["data"] == payload
    assert captured["headers"]["Content-Type"] == "application/octet-stream"
    assert captured["headers"]["Content-Length"] == str(len(payload))
    assert "Content-Range" not in captured["headers"]
