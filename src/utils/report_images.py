import re
from functools import lru_cache
from typing import Iterable, List

import requests  # kept for tests that monkeypatch report_images.requests

_JUNK_IMAGE_MARKERS = (
    "logo",
    "icon",
    "empty",
    "placeholder",
    "product_base",
    "default",
    "avatar",
    "badge",
    "flag",
    "banner",
)


def normalize_source_image_url(url: str, size: int = 800) -> str:
    url = str(url or "").strip()
    if not url:
        return ""

    if url.startswith("//"):
        url = "https:" + url

    url = url.replace("&amp;", "&")
    if "x-oss-process" in url:
        url = re.sub(
            r"x-oss-process=image%2Fresize[^&]*",
            f"x-oss-process=image%2Fresize%2Cw_{size}%2Ch_{size}%2Cm_pad",
            url,
            flags=re.IGNORECASE,
        )
    return url


def normalize_image_list(images: Iterable[str], max_images: int = 24) -> List[str]:
    normalized: List[str] = []
    seen = set()

    for raw_url in images or []:
        url = normalize_source_image_url(raw_url, size=800)
        if not url or not url.startswith("http"):
            continue

        lowered = url.lower()
        if any(marker in lowered for marker in _JUNK_IMAGE_MARKERS):
            continue

        dedupe_key = url.split("?", 1)[0]
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        normalized.append(url)
        if len(normalized) >= max_images:
            break

    return normalized


def normalize_thumbnail_url(url: str) -> str:
    url = normalize_source_image_url(url, size=120)
    if not url:
        return ""

    return (
        url
        .replace("w_800%2Ch_800", "w_120%2Ch_120")
        .replace("w_800,h_800", "w_120,h_120")
    )


@lru_cache(maxsize=256)
def inline_image_src(url: str) -> str:
    """Return a https thumbnail URL for report HTML.

    Do NOT embed data:image base64 here — that ballooned GrovePop ops emails
    to 150MB+. SMTP path still CIDs remote images via email_sender.
    """
    return normalize_thumbnail_url(url)


def build_thumbnail_img_html(url: str, width: int = 40, height: int = 40) -> str:
    src = inline_image_src(url)
    if not src:
        return ''
    return f'<img src="{src}" width="{width}" height="{height}" style="object-fit:cover;border-radius:4px;" />'


def make_ebay_listing_url(listing_id: str | None) -> str:
    listing_id = str(listing_id or '').strip()
    if not listing_id:
        return ''
    return f'https://www.ebay.com/itm/{listing_id}'
