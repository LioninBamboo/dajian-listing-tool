import base64
import logging
import mimetypes
import re
from functools import lru_cache
from typing import Iterable, List

import requests
import urllib3

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
    normalized_url = normalize_thumbnail_url(url)
    if not normalized_url:
        return ''

    try:
        response = requests.get(normalized_url, timeout=20, verify=False)
        if response.status_code != 200 or not response.content:
            return normalized_url

        content_type = (response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if not content_type.startswith('image/'):
            guessed, _ = mimetypes.guess_type(normalized_url)
            content_type = guessed or 'image/jpeg'

        encoded = base64.b64encode(response.content).decode('ascii')
        return f'data:{content_type};base64,{encoded}'
    except Exception as exc:
        logger.warning(f'报告缩略图内嵌失败: {normalized_url[:120]} ({exc})')
        return normalized_url


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
