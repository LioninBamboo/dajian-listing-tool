"""Unit tests for EbayVideoUploader download content validation (no network)."""

from __future__ import annotations

import types

import pytest

from src.services import ebay_video_uploader as vu


class _FakeResponse:
    def __init__(self, content: bytes, content_type: str, status_code: int = 200):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def get(self, url, stream=True, timeout=120):
        return self._response


def _uploader_with_download(monkeypatch, content: bytes, content_type: str) -> vu.EbayVideoUploader:
    monkeypatch.setattr(
        vu,
        "create_session_with_retry",
        lambda retries=3: _FakeSession(_FakeResponse(content, content_type)),
    )

    class _OAuth:
        environment = "PRODUCTION"

        def get_access_token(self):
            return "token"

    uploader = vu.EbayVideoUploader(_OAuth())

    def _should_not_create(*args, **kwargs):
        raise AssertionError("create_video_resource must not be called for invalid downloads")

    uploader.create_video_resource = _should_not_create  # type: ignore[method-assign]
    return uploader


def test_rejects_text_plain_small_body(monkeypatch):
    uploader = _uploader_with_download(
        monkeypatch,
        content=b"not a video file",
        content_type="text/plain",
    )
    result = uploader.upload_video_from_url("https://example.test/video.txt", "Title")
    assert result is None
    assert uploader.last_upload_error.startswith("unsupported_source")
    assert "content_type" in uploader.last_upload_error


def test_rejects_video_content_type_but_too_small(monkeypatch):
    uploader = _uploader_with_download(
        monkeypatch,
        content=b"x" * 1000,
        content_type="video/mp4",
    )
    result = uploader.upload_video_from_url("https://example.test/tiny.mp4", "Title")
    assert result is None
    assert uploader.last_upload_error.startswith("unsupported_source")
    assert "size=" in uploader.last_upload_error


def test_accepts_video_mp4_over_min_size(monkeypatch):
    payload = b"\x00\x00\x00\x18ftypmp42" + (b"\x00" * (vu.MIN_DOWNLOADABLE_VIDEO_BYTES))
    monkeypatch.setattr(
        vu,
        "create_session_with_retry",
        lambda retries=3: _FakeSession(_FakeResponse(payload, "video/mp4")),
    )

    class _OAuth:
        environment = "PRODUCTION"

        def get_access_token(self):
            return "token"

    uploader = vu.EbayVideoUploader(_OAuth())
    created = {}

    def _create(title, description, size):
        created["size"] = size
        return ("vid-1", None)

    def _upload_content(video_id, data, content_type="video/mp4"):
        created["video_id"] = video_id
        created["bytes"] = len(data)
        return True

    uploader.create_video_resource = _create  # type: ignore[method-assign]
    uploader.upload_video_content = _upload_content  # type: ignore[method-assign]

    result = uploader.upload_video_from_url("https://example.test/ok.mp4", "Title")
    assert result == "vid-1"
    assert created["size"] >= vu.MIN_DOWNLOADABLE_VIDEO_BYTES
    assert created["bytes"] >= vu.MIN_DOWNLOADABLE_VIDEO_BYTES
