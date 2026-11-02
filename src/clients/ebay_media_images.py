"""eBay Commerce Media API image upload (createImageFromUrl / createImageFromFile).

Trading UploadSiteHostedPictures was decommissioned on 2026-10-26
(release notes 1489). Publish and repair paths host images through
https://apim.ebay.com/commerce/media/v1_beta instead.

Source URLs are sent byte-identical (the only rewrite is prefixing
scheme-relative //host/... with https:). Query strings, including GigaB2B
x-cc/x-cu/x-ct/x-cs signatures, are never stripped or re-encoded here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from src.utils.store_profile import get_store_profile

MEDIA_API_BASE = "https://apim.ebay.com"
MEDIA_IMAGE_API = f"{MEDIA_API_BASE}/commerce/media/v1_beta"
CREATE_IMAGE_FROM_URL = f"{MEDIA_IMAGE_API}/image/create_image_from_url"
CREATE_IMAGE_FROM_FILE = f"{MEDIA_IMAGE_API}/image/create_image_from_file"
GET_IMAGE_URL = f"{MEDIA_IMAGE_API}/image/{{image_id}}"

SELL_INVENTORY_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory"

MAX_HTTP_ATTEMPTS = 3
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
FALLBACK_ERROR_IDS = frozenset({190201, 190202, 190203, 190204})
SCOPE_ERROR_IDS = frozenset({190013, 1100})
TOKEN_ERROR_ID = 1001
DEFAULT_MIN_POST_INTERVAL = 0.125  # ~8 POSTs/s; eBay limit is 50 per 5s per user
RETRY_AFTER_CAP_SECONDS = 30.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaImage:
    image_id: str
    image_url: str
    expiration: Optional[str] = None


class MediaImageUploadError(ValueError):
    """All-or-nothing image upload failed for one or more source URLs."""

    def __init__(
        self,
        failed: Sequence[Tuple[int, str, str]],
        uploaded_count: int = 0,
    ):
        self.failed: List[Tuple[int, str, str]] = [
            (int(index), str(prefix), str(reason)) for index, prefix, reason in failed
        ]
        self.uploaded_count = int(uploaded_count)
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        return (
            f"image upload failed: {self.uploaded_count} of "
            f"{self.uploaded_count + len(self.failed)} images: {self.reasons_text()}"
        )

    def reasons_text(self) -> str:
        parts = []
        for index, prefix, reason in self.failed:
            parts.append(f"[{index}] {prefix}: {reason}")
        return "; ".join(parts)


class MediaApiScopeError(Exception):
    """Stored user token lacks sell.inventory (or Media API refused it)."""


class _FallbackToFile(Exception):
    """createImageFromUrl cannot host this source; try createImageFromFile."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _PermanentMediaError(Exception):
    """Non-retryable Media API failure that must not fall back to file."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def apply_scheme_relative(url: str) -> str:
    """Prefix scheme-relative URLs with https:. Leave every other byte alone."""
    if isinstance(url, str) and url.startswith("//"):
        return "https:" + url
    return url


def media_url_prefix(url: str) -> str:
    """Host + path only. Never include a query string (Giga signatures)."""
    raw = apply_scheme_relative(url if isinstance(url, str) else str(url or ""))
    parsed = urlparse(raw)
    if not parsed.netloc:
        return raw.split("?", 1)[0]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _store_name() -> str:
    try:
        return str(get_store_profile().brand_name or "").strip() or "unknown-store"
    except Exception:
        return "unknown-store"


def format_media_scope_error(status_code: int, error_id: Any) -> str:
    store_name = _store_name()
    return (
        f"eBay Media API refused the stored user token for {store_name} "
        f"(HTTP {status_code}, errorId {error_id}). Required OAuth scope: "
        f"{SELL_INVENTORY_SCOPE}. Re-authorize this store via /ebay/auth so the "
        "consent includes sell.inventory, then retry. No listing was published."
    )


def _as_error_id(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_ebay_error_payload(resp: requests.Response) -> Tuple[List[int], str]:
    """Return (errorIds, combined message). Never includes headers or tokens."""
    try:
        data = resp.json()
    except Exception:
        return [], ""
    if not isinstance(data, dict):
        return [], ""

    ids: List[int] = []
    messages: List[str] = []
    for err in data.get("errors") or []:
        if not isinstance(err, dict):
            continue
        parsed_id = _as_error_id(err.get("errorId") if "errorId" in err else err.get("error_id"))
        if parsed_id is not None:
            ids.append(parsed_id)
        for key in ("message", "longMessage", "long_message", "error_description"):
            text = str(err.get(key) or "").strip()
            if text:
                messages.append(text)
                break

    oauth_error = str(data.get("error") or "").strip()
    oauth_desc = str(data.get("error_description") or data.get("errorDescription") or "").strip()
    if oauth_error:
        messages.append(oauth_error)
    if oauth_desc:
        messages.append(oauth_desc)
    return ids, " ".join(messages)


def is_scope_failure(status_code: int, error_ids: Sequence[int], message: str) -> bool:
    if int(status_code) != 403:
        return False
    if any(eid in SCOPE_ERROR_IDS for eid in error_ids):
        return True
    lowered = (message or "").lower()
    return "insufficient_scope" in lowered or "insufficient permissions" in lowered


def _retry_after_seconds(resp: requests.Response) -> Optional[float]:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _raise_if_scope_error(resp: requests.Response) -> None:
    error_ids, message = parse_ebay_error_payload(resp)
    if is_scope_failure(resp.status_code, error_ids, message):
        error_id = next((eid for eid in error_ids if eid in SCOPE_ERROR_IDS), error_ids[0] if error_ids else "unknown")
        raise MediaApiScopeError(format_media_scope_error(resp.status_code, error_id))


class EbayMediaImageUploader:
    """Bounded Media API image uploader. One instance per RealEbayClient."""

    def __init__(
        self,
        oauth,
        session: Optional[requests.Session] = None,
        min_post_interval: float = DEFAULT_MIN_POST_INTERVAL,
    ):
        self.oauth = oauth
        self.session = session or self._build_session()
        self.min_post_interval = max(0.0, float(min_post_interval))
        self._last_post_mono = 0.0

    @staticmethod
    def _build_session() -> requests.Session:
        # Dedicated session: same timeouts as RealEbayClient, no urllib3 Retry
        # so the 3-attempt policy stays exact and testable.
        from src.clients.real_ebay_client import _TimeoutSession

        session = _TimeoutSession(default_timeout=(30, 120))
        session.trust_env = False
        session.verify = False
        return session

    def _throttle_post(self) -> None:
        if self.min_post_interval <= 0:
            self._last_post_mono = time.monotonic()
            return
        now = time.monotonic()
        wait_for = self.min_post_interval - (now - self._last_post_mono)
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_post_mono = time.monotonic()

    def _sleep_backoff(self, attempt: int, retry_after: Optional[float]) -> None:
        if retry_after is not None:
            delay = min(max(retry_after, 0.0), RETRY_AFTER_CAP_SECONDS)
        else:
            index = min(max(attempt - 1, 0), len(BACKOFF_SECONDS) - 1)
            delay = BACKOFF_SECONDS[index]
        time.sleep(delay)

    def _authorization_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        token = self.oauth.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}
        if extra:
            headers.update(extra)
        return headers

    def _log(self, *, status: Any, error_id: Any, image_index: Optional[int], url: str, extra: str = "") -> None:
        parts = [
            f"status={status}",
            f"errorId={error_id}",
        ]
        if image_index is not None:
            parts.append(f"image={image_index}")
        parts.append(f"url={media_url_prefix(url)}")
        if extra:
            parts.append(extra)
        logger.info("[MEDIA] %s", " ".join(parts))

    def _request(
        self,
        method: str,
        url: str,
        *,
        image_index: Optional[int] = None,
        source_url: str = "",
        allow_token_refresh: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        method_upper = method.upper()
        last_error = "request failed"
        token_refreshed = False

        for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
            headers = self._authorization_headers(kwargs.get("headers"))
            call_kwargs = {k: v for k, v in kwargs.items() if k != "headers"}
            if method_upper == "POST":
                self._throttle_post()
            try:
                resp = self.session.request(
                    method_upper,
                    url,
                    headers=headers,
                    **call_kwargs,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                last_error = f"{type(exc).__name__}"
                self._log(
                    status="exc",
                    error_id="-",
                    image_index=image_index,
                    url=source_url or url,
                    extra=f"attempt={attempt}/{MAX_HTTP_ATTEMPTS} {last_error}",
                )
                if attempt >= MAX_HTTP_ATTEMPTS:
                    raise _PermanentMediaError(
                        f"connection/timeout after {MAX_HTTP_ATTEMPTS} attempts"
                    ) from exc
                self._sleep_backoff(attempt, None)
                continue

            error_ids, _err_message = parse_ebay_error_payload(resp)
            error_id = error_ids[0] if error_ids else "-"
            self._log(
                status=resp.status_code,
                error_id=error_id,
                image_index=image_index,
                url=source_url or url,
            )
            _raise_if_scope_error(resp)

            if resp.status_code == 401 and TOKEN_ERROR_ID in error_ids and allow_token_refresh and not token_refreshed:
                self.oauth.refresh_access_token()
                token_refreshed = True
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_HTTP_ATTEMPTS:
                self._sleep_backoff(attempt, _retry_after_seconds(resp))
                continue

            return resp

        raise _PermanentMediaError(last_error)

    def _media_image_from_response(self, resp: requests.Response, *, source_url: str) -> MediaImage:
        body: Dict[str, Any] = {}
        if resp.content:
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = {}

        image_url = str(body.get("imageUrl") or "").strip()
        expiration = str(body.get("expirationDate") or "") or None
        image_id = ""

        if not image_url:
            location = resp.headers.get("Location") or resp.headers.get("location") or ""
            image_id = str(location).rstrip("/").split("/")[-1].strip()
            if not image_id:
                raise _PermanentMediaError("201 response missing imageUrl and Location")
            fetched = self.get_image(image_id, source_url=source_url)
            image_url = fetched.image_url
            expiration = fetched.expiration or expiration
            image_id = fetched.image_id or image_id

        from src.clients.real_ebay_client import is_ebay_hosted_image_url

        if not is_ebay_hosted_image_url(image_url):
            raise _PermanentMediaError("Media API returned a non-hosted image URL")

        if not image_id:
            image_id = str(body.get("imageId") or body.get("image_id") or "").strip()
        return MediaImage(image_id=image_id, image_url=image_url, expiration=expiration)

    def get_image(self, image_id: str, *, source_url: str = "") -> MediaImage:
        resp = self._request(
            "GET",
            GET_IMAGE_URL.format(image_id=image_id),
            source_url=source_url,
        )
        if resp.status_code != 200:
            error_ids, message = parse_ebay_error_payload(resp)
            reason = f"HTTP {resp.status_code} errorId={error_ids[0] if error_ids else '-'}"
            if message:
                reason = f"{reason}: {message[:200]}"
            raise _PermanentMediaError(reason)
        body = resp.json() if resp.content else {}
        if not isinstance(body, dict):
            raise _PermanentMediaError("getImage returned a non-object body")
        image_url = str(body.get("imageUrl") or "").strip()
        from src.clients.real_ebay_client import is_ebay_hosted_image_url

        if not is_ebay_hosted_image_url(image_url):
            raise _PermanentMediaError("getImage returned a non-hosted image URL")
        return MediaImage(
            image_id=str(image_id),
            image_url=image_url,
            expiration=str(body.get("expirationDate") or "") or None,
        )

    def upload_image_from_url(self, source_url: str, *, image_index: Optional[int] = None) -> MediaImage:
        url = apply_scheme_relative(source_url)
        resp = self._request(
            "POST",
            CREATE_IMAGE_FROM_URL,
            image_index=image_index,
            source_url=url,
            json={"imageUrl": url},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code == 201:
            return self._media_image_from_response(resp, source_url=url)

        error_ids, message = parse_ebay_error_payload(resp)
        reason = f"HTTP {resp.status_code} errorId={error_ids[0] if error_ids else '-'}"
        if message:
            reason = f"{reason}: {message[:200]}"

        if resp.status_code == 400 and any(eid in FALLBACK_ERROR_IDS for eid in error_ids):
            raise _FallbackToFile(reason)
        if resp.status_code in RETRYABLE_STATUS or resp.status_code >= 500:
            raise _PermanentMediaError(reason)
        if resp.status_code == 401:
            raise _PermanentMediaError(reason)
        raise _PermanentMediaError(reason)

    def upload_image_from_file(self, source_url: str, *, image_index: Optional[int] = None) -> MediaImage:
        url = apply_scheme_relative(source_url)
        try:
            download = requests.get(url, timeout=30, stream=True, verify=False)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise _PermanentMediaError(f"download {type(exc).__name__}") from exc

        if download.status_code != 200:
            raise _PermanentMediaError(f"download HTTP {download.status_code}")

        content_type = str(download.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        if not content_type.lower().startswith("image/"):
            raise _PermanentMediaError("download Content-Type is not image/*")

        from src.clients.real_ebay_client import (
            EPS_MAX_IMAGE_BYTES,
            EPS_MIN_IMAGE_SIDE,
            normalize_eps_image_data,
        )

        image_data = download.content
        try:
            image_data, content_type, (width, height) = normalize_eps_image_data(
                image_data,
                content_type,
            )
        except Exception as exc:
            raise _PermanentMediaError("image normalization failed") from exc

        if len(image_data) > EPS_MAX_IMAGE_BYTES:
            raise _PermanentMediaError("image exceeds EPS_MAX_IMAGE_BYTES after normalization")
        if width < EPS_MIN_IMAGE_SIDE or height < EPS_MIN_IMAGE_SIDE:
            raise _PermanentMediaError(f"image too small after normalization ({width}x{height})")

        filename = "image.jpg"
        if "png" in content_type.lower():
            filename = "image.png"
        elif "gif" in content_type.lower():
            filename = "image.gif"

        resp = self._request(
            "POST",
            CREATE_IMAGE_FROM_FILE,
            image_index=image_index,
            source_url=url,
            files={"image": (filename, image_data, content_type)},
        )
        if resp.status_code == 201:
            return self._media_image_from_response(resp, source_url=url)

        error_ids, message = parse_ebay_error_payload(resp)
        reason = f"HTTP {resp.status_code} errorId={error_ids[0] if error_ids else '-'}"
        if message:
            reason = f"{reason}: {message[:200]}"
        raise _PermanentMediaError(reason)

    def upload_one(self, source_url: str, *, image_index: Optional[int] = None) -> MediaImage:
        url = apply_scheme_relative(source_url)
        if url.lower().startswith("http://"):
            return self.upload_image_from_file(url, image_index=image_index)
        try:
            return self.upload_image_from_url(url, image_index=image_index)
        except _FallbackToFile as exc:
            self._log(
                status="fallback",
                error_id="file",
                image_index=image_index,
                url=url,
                extra=exc.reason,
            )
            return self.upload_image_from_file(url, image_index=image_index)


def upload_images_via_media(
    uploader: EbayMediaImageUploader,
    image_urls: Sequence[str],
    max_images: int = 24,
) -> List[str]:
    """Upload every non-empty URL or raise MediaImageUploadError. Preserve order."""
    from src.clients.real_ebay_client import is_ebay_hosted_image_url

    targets = list(image_urls or [])[:max_images]
    hosted: List[str] = []
    failed: List[Tuple[int, str, str]] = []

    for index, raw in enumerate(targets):
        if raw is None:
            continue
        url = raw if isinstance(raw, str) else str(raw)
        if not url.strip():
            continue
        if is_ebay_hosted_image_url(url):
            hosted.append(url)
            continue
        try:
            media = uploader.upload_one(url, image_index=index)
            hosted.append(media.image_url)
        except MediaApiScopeError:
            raise
        except (_PermanentMediaError, _FallbackToFile) as exc:
            failed.append((index, media_url_prefix(url), str(exc)))
            raise MediaImageUploadError(failed=failed, uploaded_count=len(hosted)) from exc
        except Exception as exc:
            failed.append((index, media_url_prefix(url), type(exc).__name__))
            raise MediaImageUploadError(failed=failed, uploaded_count=len(hosted)) from exc

    if failed:
        raise MediaImageUploadError(failed=failed, uploaded_count=len(hosted))
    return hosted
