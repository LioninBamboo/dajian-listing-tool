"""
Real eBay API Client

Implements complete eBay listing workflow using:
- Inventory API: Product upload
- Account API: Policy management  
- Offer API: Listing creation and publishing
- Media API: Video upload
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import os
import logging
import re
import time
import urllib3
from urllib.parse import urlparse, parse_qs, urlencode
from typing import Dict, List, Optional, Any
from src.services.ebay_auth import EbayOAuthService
from src.services.ebay_policy_manager import EbayPolicyManager
from src.utils.publish_autofix import (
    EBAY_MAX_ASPECT_VALUE_LEN,
    SINGLE_VALUE_ASPECTS,
    sanitize_single_value_aspects,
    prepare_ebay_aspects,
    source_combined_aspect_keys,
)
from src.utils.title_sanitizer import normalize_listing_title_for_ebay
from src.utils.store_profile import get_store_profile

# Suppress InsecureRequestWarning when verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EBAY_HOSTED_IMAGE_DOMAINS = ("i.ebayimg.com",)
EPS_MAX_IMAGE_BYTES = 10 * 1024 * 1024
EPS_TARGET_MAX_IMAGE_BYTES = 9_500_000
EPS_MIN_IMAGE_SIDE = 500
# eBay EPS rejects uploads whose longest side exceeds 15000 pixels
# (Trading error 21916604). Stay slightly under the hard limit.
EPS_MAX_IMAGE_SIDE = 14000
# eBay also rejects when width + height exceeds 15000 (same error code).
# Square 8335x8335 Giga assets hit this even though each side is under 15000.
EPS_MAX_COMBINED_DIMENSION = 14900
UNBRANDED_MARKERS = {"unbranded", "unbrand", "generic"}
VALID_ASSEMBLY_STATUS_VALUES = {"Part Assembled", "Fully Assembled", "Ready to Assemble"}
INVALID_MOTORS_COMPATIBILITY_ERROR_CODES = {"21916723", "21916724"}
IDENTIFIER_PATTERNS = {
    "upc": r"\d{12}",
    "ean": r"(?:\d{8}|\d{13})",
    "isbn": r"(?:\d{9}[\dXx]|\d{13})",
}
# eBay US requires this exact substitute when a category requires a product
# identifier but the product genuinely has none.
# https://developer.ebay.com/api-docs/sell/static/inventory/product-identifier-text.html
PRODUCT_IDENTIFIER_UNAVAILABLE_TEXT = "Does not apply"


def stored_video_id_from_optimization(opt: Any) -> str:
    """Return the locally recorded eBay video id from an optimization blob."""
    if not isinstance(opt, dict):
        return ""
    video_id = str(opt.get("video_id") or opt.get("videoId") or "").strip()
    if video_id:
        return video_id
    video_ids = opt.get("videoIds")
    if isinstance(video_ids, list):
        for item in video_ids:
            text = str(item or "").strip()
            if text:
                return text
        return ""
    if isinstance(video_ids, str):
        return video_ids.strip()
    return ""


def lookup_stored_video_id(sku: str) -> str:
    """Best-effort local DB lookup of a previously linked eBay video id."""
    sku = str(sku or "").strip()
    if not sku:
        return ""
    try:
        import sqlite3
        from pathlib import Path

        db_path = Path(__file__).resolve().parents[2] / "ebay_collection.db"
        if not db_path.exists():
            return ""
        con = sqlite3.connect(str(db_path))
        try:
            row = con.execute(
                "SELECT optimization FROM collected_products WHERE sku = ?",
                (sku,),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return ""
        opt = json.loads(row[0] or "{}") if isinstance(row[0], str) else (row[0] or {})
        return stored_video_id_from_optimization(opt)
    except Exception:
        return ""


def _first_text_value(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = str(item).strip()
            if text:
                return text
        return ""
    return str(value).strip() if value is not None else ""


def _normalize_offer_description_for_compare(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sanitize_inventory_identifiers(
    sku: str,
    cleaned_aspects: Dict[str, List[str]],
    product: Dict[str, Any],
) -> tuple[str, str, dict[str, list[str]]]:
    brand = str(product.get("brand") or _first_text_value(cleaned_aspects.get("Brand"))).strip()
    mpn = str(product.get("mpn") or _first_text_value(cleaned_aspects.get("MPN"))).strip()

    if brand.lower() in UNBRANDED_MARKERS and not mpn:
        mpn = "Does Not Apply"
        cleaned_aspects["MPN"] = [mpn]

    identifiers: dict[str, list[str]] = {}
    for field_name, pattern in IDENTIFIER_PATTERNS.items():
        explicit = product.get(field_name)
        aspect_value = _first_text_value(cleaned_aspects.get(field_name.upper()))
        identifier = explicit or aspect_value
        if not identifier:
            cleaned_aspects.pop(field_name.upper(), None)
            continue

        raw_values = identifier if isinstance(identifier, list) else [identifier]
        valid_values: list[str] = []
        for raw_value in raw_values:
            raw_text = str(raw_value or "").strip()
            if raw_text.casefold() == PRODUCT_IDENTIFIER_UNAVAILABLE_TEXT.casefold():
                valid_values = [PRODUCT_IDENTIFIER_UNAVAILABLE_TEXT]
                break
            normalized = re.sub(r"[-\s]", "", raw_text)
            if not normalized or normalized.upper() == str(sku or "").strip().upper():
                continue
            if not re.fullmatch(pattern, normalized):
                continue
            valid_values.append(normalized)

        if valid_values:
            identifiers[field_name] = valid_values
            cleaned_aspects[field_name.upper()] = valid_values
        else:
            cleaned_aspects.pop(field_name.upper(), None)

    return brand, mpn, identifiers


def _sanitize_assembly_aspects(cleaned_aspects: Dict[str, List[str]]) -> None:
    assembly_status = cleaned_aspects.get("Assembly Status")
    if not assembly_status:
        return

    normalized_values = [str(item).strip() for item in assembly_status if str(item).strip()]
    if not normalized_values:
        cleaned_aspects.pop("Assembly Status", None)
        return

    if normalized_values[0] not in VALID_ASSEMBLY_STATUS_VALUES:
        cleaned_aspects.pop("Assembly Status", None)


def normalize_eps_image_data(
    image_data: bytes,
    content_type: str = "image/jpeg",
    *,
    max_bytes: int = EPS_TARGET_MAX_IMAGE_BYTES,
    min_side: int = EPS_MIN_IMAGE_SIDE,
    max_side: int = EPS_MAX_IMAGE_SIDE,
    max_combined: int = EPS_MAX_COMBINED_DIMENSION,
) -> tuple[bytes, str, tuple[int, int]]:
    """Prepare source image bytes for eBay EPS constraints."""
    from io import BytesIO
    from PIL import Image

    with Image.open(BytesIO(image_data)) as img:
        width, height = img.size
        needs_reencode = (
            len(image_data) > max_bytes
            or width < min_side
            or height < min_side
            or width > max_side
            or height > max_side
            or (width + height) > max_combined
            or content_type.lower() not in {"image/jpeg", "image/jpg", "image/png"}
        )

        if not needs_reencode:
            return image_data, content_type, (width, height)

        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            background = Image.new("RGB", img.size, "white")
            background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = background
        else:
            img = img.convert("RGB")

        width, height = img.size
        if width < min_side or height < min_side:
            scale = max(min_side / max(width, 1), min_side / max(height, 1))
            img = img.resize((int(width * scale + 0.5), int(height * scale + 0.5)), Image.LANCZOS)
            width, height = img.size
        if width > max_side or height > max_side or (width + height) > max_combined:
            scale = min(
                max_side / max(width, 1),
                max_side / max(height, 1),
                max_combined / max(width + height, 1),
            )
            img = img.resize((int(width * scale + 0.5), int(height * scale + 0.5)), Image.LANCZOS)
            width, height = img.size

        quality = 92
        while True:
            out = BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            encoded = out.getvalue()
            if len(encoded) <= max_bytes or quality <= 70:
                return encoded, "image/jpeg", (width, height)
            quality -= 7


def clean_image_url(url: str) -> str:
    """
    Clean image URL to get original full-size image.
    
    eBay API (errorId 25721) rejects some image transformation parameters.
    GigaB2B image URLs also carry signed access parameters in the query string,
    so only remove the known resize/processing parameter and preserve the
    signature fields.
    
    Args:
        url: Raw image URL (may contain CDN processing parameters)
        
    Returns:
        Clean URL without query parameters
    """
    # 1. 验证 URL 非空
    if not url or not isinstance(url, str):
        return ""
    
    url = url.strip()
    
    # 2. 处理协议头
    if url.startswith("//"):
        url = "https:" + url
    
    # 3. 验证是 HTTP/HTTPS URL
    if not url.startswith(("http://", "https://")):
        return ""
    
    # 4. 移除图片处理参数，但保留签名参数。
    # GigaB2B 的 x-cc/x-cu/x-ct/x-cs 是访问签名；删掉后 eBay 后续刷新
    # 库存项时可能只保留 1 张可访问图片。
    if "?" in url:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        params.pop("x-oss-process", None)
        new_query = urlencode({k: v[0] for k, v in params.items() if v})
        url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if new_query:
            url = f"{url}?{new_query}"
    
    # 5. 移除 URL 路径中的缩略图参数
    # Pattern: /w_74,h_74/ or similar
    url = re.sub(r'/w_\d+,h_\d+[^/]*/', '/', url)
    # Pattern: _128x128.jpg -> .jpg
    url = re.sub(r'_\d+x\d+\.', '.', url)
    
    # 5.5 Convert eBay-hosted tiny thumbnails to large versions
    # $_1.JPG = 74px gallery thumbnail (fails eBay's 500px minimum)
    # $_57.JPG = large version (~500-800px, acceptable)
    if 'i.ebayimg.com' in url:
        url = re.sub(r'\$_1\.(JPG|PNG|jpg|png)', r'$_57.\1', url)
    
    # 6. 验证 URL 结构
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return ""
    except:
        return ""
    
    return url


def is_ebay_hosted_image_url(url: str) -> bool:
    """Return True when the URL already points to eBay-hosted image infrastructure."""
    clean_url = clean_image_url(url)
    if not clean_url:
        return False

    host = (urlparse(clean_url).netloc or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in EBAY_HOSTED_IMAGE_DOMAINS)


def normalize_inventory_image_urls(image_urls: List[str], max_images: int = 24) -> List[str]:
    """Clean, dedupe, and clamp image URLs while preserving order."""
    normalized = []
    seen = set()

    for raw_url in image_urls or []:
        clean_url = clean_image_url(raw_url)
        if not clean_url:
            continue

        key = clean_url.casefold()
        if key in seen:
            continue

        seen.add(key)
        normalized.append(clean_url)

        if len(normalized) >= max_images:
            break

    return normalized

# Session with enforced timeouts — requests.Session doesn't honour .timeout attr
class _TimeoutSession(requests.Session):
    """Session that applies a default (connect, read) timeout to every request."""

    def __init__(self, default_timeout=(30, 120)):
        super().__init__()
        self._default_timeout = default_timeout

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(*args, **kwargs)


def create_ebay_session():
    """Create requests session with eBay-specific settings, retry logic, and enforced timeouts."""
    session = _TimeoutSession(default_timeout=(30, 120))
    # Disable proxy for eBay domains
    session.trust_env = False
    # Disable SSL verification to work around intermittent SSL EOF errors
    session.verify = False

    # Configure retry strategy for connection errors + SSL errors
    retry_strategy = Retry(
        total=5,
        connect=5,
        backoff_factor=2,  # Wait 2, 4, 8, 16, 32 seconds between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "PUT", "POST", "DELETE", "OPTIONS", "TRACE"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


class RealEbayClient:
    """Real eBay API Client for product listing"""
    
    def __init__(self, oauth_service: EbayOAuthService, policy_manager: EbayPolicyManager):
        """
        Initialize Real eBay Client
        
        Args:
            oauth_service: EbayOAuthService for authentication
            policy_manager: EbayPolicyManager for policy IDs
        """
        self.oauth = oauth_service
        self.policy_manager = policy_manager
        self.base_url = oauth_service.api_base
        # Per-instance: auto-parts instances list on EBAY_MOTORS_US, everything
        # else on EBAY_US (the default).
        self.marketplace_id = get_store_profile().ebay_marketplace_id
        self.session = create_ebay_session()

    def _complete_listing_policies(self, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return listingPolicies with required business policy IDs present."""
        policies = dict(existing or {})
        try:
            cached = {
                "fulfillmentPolicyId": self.policy_manager.get_default_fulfillment_policy_id(),
                "returnPolicyId": self.policy_manager.get_default_return_policy_id(),
                "paymentPolicyId": self.policy_manager.get_default_payment_policy_id(),
            }
        except Exception:
            cached = {}

        for key, fallback_value in get_store_profile().fallback_listing_policies().items():
            if policies.get(key):
                continue
            policies[key] = cached.get(key) or fallback_value

        return policies

    def _prepare_inventory_image_urls(self, sku: str, image_urls: List[str], max_images: int = 24) -> List[str]:
        """
        Prepare a stable image set for inventory PUTs.

        Rules:
        - Reuse eBay-hosted URLs as-is.
        - Convert external supplier URLs to EPS-hosted URLs before PUT.
        - Refuse to overwrite a multi-image listing with only a single stable URL.
        """
        cleaned_urls = normalize_inventory_image_urls(image_urls, max_images=max_images)
        if not cleaned_urls:
            return []

        if all(is_ebay_hosted_image_url(url) for url in cleaned_urls):
            return cleaned_urls

        live_inventory = self.get_inventory_item(sku) or {}
        live_image_urls = normalize_inventory_image_urls(
            (live_inventory.get("product") or {}).get("imageUrls") or [],
            max_images=max_images,
        )
        live_hosted_urls = [url for url in live_image_urls if is_ebay_hosted_image_url(url)]

        logging.info(
            f"[IMAGES] {sku}: converting {len(cleaned_urls)} source image URLs to eBay EPS before inventory PUT"
        )
        eps_urls = normalize_inventory_image_urls(
            self.upload_images_to_eps(cleaned_urls, max_images=max_images),
            max_images=max_images,
        )

        chosen_urls = eps_urls if len(eps_urls) >= len(live_hosted_urls) else live_hosted_urls
        if not chosen_urls:
            raise ValueError(f"{sku}: unable to prepare stable eBay-hosted image URLs")

        if len(cleaned_urls) >= 2 and len(chosen_urls) < 2:
            raise ValueError(
                f"{sku}: refusing to update inventory with only {len(chosen_urls)} stable image URL(s) "
                f"from {len(cleaned_urls)} source image(s)"
            )

        if len(chosen_urls) < len(cleaned_urls):
            logging.warning(
                f"[IMAGES] {sku}: prepared {len(chosen_urls)}/{len(cleaned_urls)} stable image URLs; "
                "keeping the largest verified hosted set"
            )

        return chosen_urls

    def _verify_inventory_image_urls(self, sku: str, expected_count: int) -> None:
        """Read back inventory imageUrls and fail fast on severe image collapse."""
        if expected_count <= 0:
            return

        live_inventory = self.get_inventory_item(sku) or {}
        live_image_urls = normalize_inventory_image_urls(
            (live_inventory.get("product") or {}).get("imageUrls") or []
        )
        live_count = len(live_image_urls)

        if expected_count >= 2 and live_count < 2:
            raise RuntimeError(
                f"{sku}: inventory imageUrls collapsed to {live_count}/{expected_count} after update"
            )

        if live_count < expected_count:
            logging.warning(
                f"[IMAGES] {sku}: inventory readback returned {live_count}/{expected_count} image URLs"
            )
    
    def get_inventory_item(self, sku: str) -> Optional[Dict]:
        """
        Get inventory item details
        
        GET /sell/inventory/v1/inventory_item/{sku}
        
        Returns:
            Inventory item dict or None if not found
        """
        url = f"{self.base_url}/sell/inventory/v1/inventory_item/{sku}"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        try:
            response = self.session.get(url, headers=headers, timeout=30)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            logging.error(f"get_inventory_item({sku}): {e}")
            return None

    def get_offer(self, offer_id: str) -> Optional[Dict]:
        """
        Get offer details.

        GET /sell/inventory/v1/offer/{offerId}
        """
        url = f"{self.base_url}/sell/inventory/v1/offer/{offer_id}"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        try:
            response = self.session.get(url, headers=headers, timeout=60)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                return None
            logging.warning(f"get_offer({offer_id}): HTTP {response.status_code}")
            return None
        except Exception as e:
            logging.error(f"get_offer({offer_id}): {e}")
            return None
    
    def create_or_replace_inventory_item(self, sku: str, product: Dict) -> Dict:
        """
        Create or update inventory item
        
        PUT /sell/inventory/v1/inventory_item/{sku}
        
        Args:
            sku: Product SKU
            product: Product data dict with:
                - title: str
                - description: str (HTML)
                - image_urls: List[str]
                - video_urls: List[str] (optional)
                - price: float
                - quantity: int
                - condition: str (default: "NEW")
                - aspects: Dict[str, List[str]]
                
        Returns:
            Response dict
        """
        url = f"{self.base_url}/sell/inventory/v1/inventory_item/{sku}"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US"
        }
        
        # Build payload
        # eBay limits: title 80 chars, description 4000 chars, video 1
        description = product.get("description", "")
        prepared_image_urls = self._prepare_inventory_image_urls(
            sku,
            product.get("image_urls", []),
            max_images=24,
        )
        
        # Truncate description if too long (eBay limit: 4000 chars).
        # Must preserve store footer markers (banner/footer QC contract).
        if len(description) > 4000:
            print(f"[WARN] Description is {len(description)} chars (limit: 4000), truncating...")
            try:
                from src.utils.html_truncator import smart_truncate_html

                description = smart_truncate_html(
                    description, max_length=4000, min_length=3600
                )
            except Exception:
                description = description[:3900]
                if "</div>" not in description[-100:]:
                    description = description + "</div>"
                description = description + "</div>"
        
        # Validate aspects format (must be list of strings)
        aspects = product.get("aspects", {})
        required_aspect_names = {
            str(name).strip()
            for name in (product.get("required_aspect_names") or [])
            if str(name).strip()
        }
        cleaned_aspects = {}
        for k, v in aspects.items():
            if isinstance(v, str):
                value = v.strip()
                cleaned_aspects[k] = [value] if value else []
            elif isinstance(v, list):
                values = []
                seen_values = set()
                for item in v:
                    value = str(item).strip()
                    if not value:
                        continue
                    normalized = value.casefold()
                    if normalized in seen_values:
                        continue
                    seen_values.add(normalized)
                    values.append(value)
                cleaned_aspects[k] = values
            else:
                value = str(v).strip()
                cleaned_aspects[k] = [value] if value else []

        # Enforce single-value aspects and run pre-flight truncation/trimming
        preserved_source_aspects = source_combined_aspect_keys(cleaned_aspects)
        sanitize_single_value_aspects(
            cleaned_aspects,
            log=print,
            multi_value_aspects={key for key in cleaned_aspects if key not in SINGLE_VALUE_ASPECTS}
            | preserved_source_aspects,
        )
        _sanitize_assembly_aspects(cleaned_aspects)
        prepare_ebay_aspects(cleaned_aspects, required_aspect_names, log=print)

        safe_title, _ = normalize_listing_title_for_ebay(
            product.get("title", ""),
            source_title=product.get("title", ""),
        )

        payload = {
            "condition": product.get("condition", "NEW"),
            "availability": {
                "shipToLocationAvailability": {
                    "quantity": product.get("quantity", 1)
                }
            },
            "product": {
                "title": safe_title,
                "description": description,
                "imageUrls": prepared_image_urls,
                "aspects": cleaned_aspects
            }
        }
        product_node = payload["product"]

        brand, mpn, identifiers = _sanitize_inventory_identifiers(sku, cleaned_aspects, product)
        epid = str(
            product.get("epid")
            or _first_text_value(cleaned_aspects.get("ePID"))
            or _first_text_value(cleaned_aspects.get("EPID"))
        ).strip()
        if brand:
            product_node["brand"] = brand
        if mpn:
            product_node["mpn"] = mpn
        if epid:
            product_node["epid"] = epid

        for field_name, values in identifiers.items():
            product_node[field_name] = values
        
        # Add packageWeightAndSize if provided (shipping dimensions & weight)
        # Sanitize weight/dimension values to prevent eBay 25709 errors
        pkg = product.get("packageWeightAndSize")
        if pkg:
            pkg = dict(pkg)  # shallow copy
            # Validate weight
            w = pkg.get("weight", {})
            if w:
                try:
                    val = float(w.get("value", 0))
                    if val <= 0 or val > 2000:
                        logging.warning(f"[WARN] {sku}: Invalid weight {val}, removing weight")
                        pkg.pop("weight", None)
                    else:
                        pkg["weight"] = {"value": round(val, 2), "unit": w.get("unit", "POUND")}
                except (ValueError, TypeError):
                    logging.warning(f"[WARN] {sku}: Non-numeric weight value, removing weight")
                    pkg.pop("weight", None)
            # Validate dimensions
            for dim_key in ("dimensions", "packageDimensions"):
                d = pkg.get(dim_key, {})
                if d:
                    cleaned_dim = {"unit": d.get("unit", "INCH")}
                    valid = True
                    for axis in ("length", "width", "height"):
                        try:
                            v = float(d.get(axis, 0))
                            if v <= 0 or v > 999:
                                valid = False
                                break
                            cleaned_dim[axis] = round(v, 2)
                        except (ValueError, TypeError):
                            valid = False
                            break
                    if valid:
                        pkg[dim_key] = cleaned_dim
                    else:
                        logging.warning(f"[WARN] {sku}: Invalid {dim_key}, removing")
                        pkg.pop(dim_key, None)
            if pkg:  # still has some valid data
                payload["packageWeightAndSize"] = pkg
        
        # Note: Country comes from merchantLocationKey in offer, not inventory item
        
        # Preserve existing live videoIds unless the caller explicitly overrides them.
        # If live inventory already lost the video, fall back to the locally stored
        # eBay video_id so later aspect/image PUTs cannot wipe a previously linked video.
        video_urls = product.get("video_urls")
        if video_urls:
            payload["product"]["videoIds"] = [
                str(item).strip() for item in list(video_urls) if str(item).strip()
            ][:1]
        elif "video_urls" not in product:
            try:
                live_inventory = self.get_inventory_item(sku) or {}
            except Exception:
                live_inventory = {}
            live_video_ids = [
                str(item).strip()
                for item in ((live_inventory.get("product") or {}).get("videoIds") or [])
                if str(item).strip()
            ]
            local_video_id = lookup_stored_video_id(sku)
            preserved = live_video_ids[:1] or ([local_video_id] if local_video_id else [])
            if preserved:
                payload["product"]["videoIds"] = preserved

        response = self.session.put(url, headers=headers, json=payload)
        
        if response.status_code in (200, 204):
            # HTTP 200 = success with warnings; HTTP 204 = success (no content)
            if response.status_code == 200:
                try:
                    body = response.json()
                    warnings = body.get("warnings", [])
                    if warnings:
                        for w in warnings:
                            logging.info(f"[WARN] {sku}: {w.get('message', '')}")
                except Exception:
                    pass
            self._verify_inventory_image_urls(sku, len(prepared_image_urls))
            logging.info(f"[SUCCESS] Inventory item created/updated: {sku}")
            return {"status": "success", "sku": sku}
        elif response.status_code == 400:
            # 解析并显示详细错误
            error_detail = response.text
            logging.error(f"[ERROR] eBay API 400 Bad Request: {error_detail}")
            try:
                error_json = response.json()
                errors = error_json.get("errors", [])
                for err in errors:
                    msg = err.get("message", "")
                    long_msg = err.get("longMessage", "")
                    logging.error(f"   - {msg}: {long_msg}")
            except:
                pass
            raise Exception(f"eBay API Error: {error_detail[:500]}")
        else:
            logging.error(f"[ERROR] eBay API Error {response.status_code}")
            logging.error(f"   Response: {response.text}")
            response.raise_for_status()
            return response.json()

    def get_product_compatibility(self, sku: str) -> Dict:
        """
        Get product compatibility records for an inventory item.

        GET /sell/inventory/v1/inventory_item/{sku}/product_compatibility
        """
        url = f"{self.base_url}/sell/inventory/v1/inventory_item/{sku}/product_compatibility"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        try:
            response = self.session.get(url, headers=headers, timeout=60)
            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                return {"compatibleProducts": []}
            logging.warning(f"[COMPAT] get_product_compatibility({sku}) -> HTTP {response.status_code}")
            return {"compatibleProducts": []}
        except Exception as e:
            logging.error(f"[COMPAT] Failed to get compatibility for {sku}: {e}")
            return {"compatibleProducts": []}

    def create_or_replace_product_compatibility(self, sku: str, compatible_products: List[Dict]) -> Dict:
        """
        Create or replace product compatibility records for an inventory item.

        PUT /sell/inventory/v1/inventory_item/{sku}/product_compatibility
        """
        url = f"{self.base_url}/sell/inventory/v1/inventory_item/{sku}/product_compatibility"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
            "Accept": "application/json",
        }
        payload = {"compatibleProducts": compatible_products or []}

        response = self.session.put(url, headers=headers, json=payload, timeout=120)

        if response.status_code in (200, 201, 204):
            logging.info(f"[COMPAT] Compatibility updated for {sku}: {len(compatible_products)} entries")
            return {"status": "success", "sku": sku, "count": len(compatible_products)}

        if response.status_code == 400:
            logging.error(f"[COMPAT] eBay compatibility update failed for {sku}: {response.text[:500]}")
            raise Exception(f"Compatibility update failed: {response.text[:300]}")

        response.raise_for_status()
        return response.json() if response.text else {}

    def delete_product_compatibility(self, sku: str) -> bool:
        """
        Delete product compatibility records for an inventory item.

        DELETE /sell/inventory/v1/inventory_item/{sku}/product_compatibility
        """
        url = f"{self.base_url}/sell/inventory/v1/inventory_item/{sku}/product_compatibility"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        try:
            response = self.session.delete(url, headers=headers, timeout=60)
            if response.status_code in (200, 204, 404):
                logging.info(f"[COMPAT] Compatibility cleared for {sku}")
                return True
            logging.warning(f"[COMPAT] delete_product_compatibility({sku}) -> HTTP {response.status_code}")
            return False
        except Exception as e:
            logging.error(f"[COMPAT] Failed to clear compatibility for {sku}: {e}")
            return False
    
    def create_offer(self, sku: str, price: float, category_id: Optional[str] = None, listing_description: Optional[str] = None, marketplace_id: Optional[str] = None) -> Dict:
        """
        Create offer for inventory item
        
        POST /sell/inventory/v1/offer
        
        Args:
            sku: Product SKU
            price: Listing price
            category_id: eBay category ID (optional, will use suggested if not provided)
            listing_description: HTML description for the live listing (optional)
            
        Returns:
            {"offerId": "...", "status": "..."}
        """
        url = f"{self.base_url}/sell/inventory/v1/offer"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US"
        }
        
        target_marketplace = marketplace_id or self.marketplace_id

        payload = {
            "sku": sku,
            "marketplaceId": target_marketplace,
            "format": "FIXED_PRICE",
            "merchantLocationKey": get_store_profile().merchant_location_key,
            "pricingSummary": {
                "price": {
                    "value": str(price),
                    "currency": "USD"
                }
            }
        }
        
        payload["listingPolicies"] = self._complete_listing_policies()
        print(f"[OK] Using listing policies")
        
        # Add category if provided
        if category_id:
            payload["categoryId"] = category_id
        
        # Add listing description if provided
        if listing_description:
            payload["listingDescription"] = listing_description
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.session.post(url, headers=headers, json=payload, timeout=60)
                
                if response.status_code in [200, 201]:
                    result = response.json()
                    logging.info(f"[SUCCESS] Offer created: {result.get('offerId')}")
                    return result
                elif response.status_code == 400:
                    # Check for specific errors
                    try:
                        error_data = response.json()
                        errors = error_data.get("errors", [])
                        recreate_offer = False
                        for error in errors:
                            if error.get("errorId") == 25002:
                                # Offer already exists - update with correct category
                                offer_id = None
                                for param in error.get("parameters", []):
                                    if param.get("name") == "offerId":
                                        offer_id = param.get("value")
                                        break

                                if not offer_id:
                                    continue

                                print(f"[INFO] Offer already exists: {offer_id}")
                                existing_offer = self.get_offer(offer_id) or {}
                                existing_marketplace = existing_offer.get("marketplaceId")
                                offer_status = existing_offer.get("status", "")
                                listing_status = (existing_offer.get("listing") or {}).get("listingStatus", "")

                                if (
                                    existing_marketplace
                                    and existing_marketplace != target_marketplace
                                    and listing_status != "ACTIVE"
                                ):
                                    print(
                                        f"[FIX] Existing offer {offer_id} uses marketplace {existing_marketplace}; "
                                        f"recreating in {target_marketplace}"
                                    )
                                    if not self.delete_offer(offer_id):
                                        raise Exception(
                                            f"Existing offer {offer_id} uses marketplace {existing_marketplace}, "
                                            "but deletion failed"
                                        )
                                    recreate_offer = True
                                    break

                                # Update offer with correct category if provided
                                if category_id:
                                    print(f"[INFO] Updating existing offer with category: {category_id}")
                                    try:
                                        self.update_offer_category(
                                            offer_id,
                                            category_id,
                                            price,
                                            listing_description=listing_description,
                                        )
                                    except Exception as update_e:
                                        print(f"[WARN] Failed to update existing offer category: {update_e}")
                                        # Continue to return offerId so we can handle it downstream (e.g. delete it)

                                return {
                                    "offerId": offer_id,
                                    "status": "EXISTING",
                                    "marketplaceId": existing_marketplace or target_marketplace,
                                    "offerStatus": offer_status,
                                    "listingStatus": listing_status,
                                }
                        if recreate_offer:
                            time.sleep(1)
                            continue
                    except ValueError:
                        pass
                    
                    # Other 400 error
                    logging.error(f"[ERROR] Create Offer Failed: {response.status_code}")
                    logging.error(f"[ERROR] Response Body: {response.text[:500]}")
                    raise Exception(f"Create offer failed: {response.text[:300]}")
                else:
                    response.raise_for_status()
                    
            except requests.exceptions.ConnectionError as e:
                logging.error(f"[ERROR] Connection error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    print(f"[WARN] Connection error, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"Connection failed after {max_retries} attempts: {e}")
            except requests.exceptions.Timeout as e:
                logging.error(f"[ERROR] Timeout on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    print(f"[WARN] Timeout, retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"Request timed out after {max_retries} attempts")
        
        raise Exception(f"Create offer failed after {max_retries} attempts")
    
    def create_or_replace_inventory_item_group(self, group_key: str, group: Dict) -> Dict:
        """Define a multi-variation group (blind-box series).

        PUT /sell/inventory/v1/inventory_item_group/{inventoryItemGroupKey}

        ``group`` is the full payload: title/description/imageUrls/aspects plus
        variesBy (the aspect that varies + its values) and variantSKUs. All the
        variant inventory items must already exist.
        """
        url = f"{self.base_url}/sell/inventory/v1/inventory_item_group/{group_key}"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
        }
        response = self.session.put(url, headers=headers, json=group, timeout=60)
        if response.status_code in (200, 201, 204):
            return {"status": "ok", "groupKey": group_key}
        raise Exception(
            f"inventory_item_group failed ({response.status_code}): {response.text[:400]}"
        )

    def publish_by_inventory_item_group(self, group_key: str, marketplace_id: Optional[str] = None) -> Dict:
        """Publish all variations as ONE multi-variation listing.

        POST /sell/inventory/v1/offer/publish_by_inventory_item_group
        Returns {"listingId": ...}. Every variant SKU needs an (unpublished) offer.
        """
        url = f"{self.base_url}/sell/inventory/v1/offer/publish_by_inventory_item_group"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Language": "en-US",
        }
        payload = {
            "inventoryItemGroupKey": group_key,
            "marketplaceId": marketplace_id or self.marketplace_id,
        }
        response = self.session.post(url, headers=headers, json=payload, timeout=120)
        if response.status_code == 200:
            return response.json()
        raise Exception(
            f"publish_by_inventory_item_group failed ({response.status_code}): {response.text[:400]}"
        )

    def add_fixed_price_item_motors(self, product: Dict, *, category_id: str,
                                    price: float, quantity: int = 5) -> Dict:
        """Publish an eBay Motors parts listing via the Trading API (SiteID 100).

        Motors categories can't be published through the Inventory API (25005),
        so this posts AddFixedPriceItem. ``product`` carries title/description/
        image_urls/aspects and optional ``compatibility`` (the collected
        motorsCompatibility.compatibleProducts). Returns {"itemId", "status"} or
        raises with eBay's error messages.
        """
        import re
        from src.services.motors_trading import build_add_fixed_price_item_xml
        from src.utils.store_profile import get_store_profile

        profile = get_store_profile()
        xml = build_add_fixed_price_item_xml(
            title=product.get("title", ""),
            description=product.get("description", ""),
            category_id=str(category_id),
            price=price,
            quantity=quantity,
            policies=profile.motors_listing_policies(),   # seller-paid returns (Motors P&A mandate)
            location=profile.warehouse_location,
            postal_code=profile.warehouse_postal,
            aspects=product.get("aspects") or {},
            image_urls=product.get("image_urls") or product.get("images") or [],
            compatibility=product.get("compatibility") or [],
        )

        token = self.oauth.get_valid_token()
        headers = {
            "X-EBAY-API-CALL-NAME": "AddFixedPriceItem",
            "X-EBAY-API-SITEID": profile.ebay_site_id,   # 100 = eBay Motors
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml",
        }
        resp = self.session.post("https://api.ebay.com/ws/api.dll", headers=headers,
                                 data=xml.encode("utf-8"), timeout=90)
        body = resp.text
        ack = (re.search(r"<Ack>(.*?)</Ack>", body) or [None, ""])[1] if "<Ack>" in body else ""
        error_codes = set(re.findall(r"<ErrorCode>(.*?)</ErrorCode>", body, re.S))
        item_id = (re.search(r"<ItemID>(.*?)</ItemID>", body) or [None, None])[1]
        if (
            item_id
            and ack in ("Success", "Warning")
            and not error_codes.intersection(INVALID_MOTORS_COMPATIBILITY_ERROR_CODES)
        ):
            logging.info(f"[MOTORS] Trading publish OK: ItemID {item_id} (Ack={ack})")
            return {"itemId": item_id, "status": "published", "ack": ack}
        errors = [
            (m.group(1) or "").strip()
            for m in re.finditer(r"<LongMessage>(.*?)</LongMessage>", body, re.S)
        ]
        raise Exception(f"Motors AddFixedPriceItem failed (Ack={ack}): {'; '.join(errors)[:400]}")

    def get_item_description_motors(self, item_id: str) -> str:
        """Fetch the live Description of a Trading item (Motors) — read-only."""
        import re
        from src.utils.store_profile import get_store_profile
        profile = get_store_profile()
        xml = (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
            "<GetItemRequest xmlns=\"urn:ebay:apis:eBLBaseComponents\">"
            f"<ItemID>{item_id}</ItemID><DetailLevel>ItemReturnDescription</DetailLevel>"
            "</GetItemRequest>"
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": profile.ebay_site_id,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": self.oauth.get_valid_token(),
            "Content-Type": "text/xml",
        }
        resp = self.session.post("https://api.ebay.com/ws/api.dll", headers=headers,
                                 data=xml.encode("utf-8"), timeout=60)
        m = re.search(r"<Description>(.*?)</Description>", resp.text, re.S)
        return (m.group(1) if m else "")

    def get_item_compatibility_motors(self, item_id: str) -> list[dict]:
        """Fetch the manual Trading compatibility rows for a live Motors item.

        ``IncludeItemCompatibilityList`` is required because GetItem omits the
        row details by default and only returns a count.  The result is kept in
        the same shape consumed by ``format_compatibility_list``.
        """
        import html
        import re
        from src.utils.store_profile import get_store_profile

        profile = get_store_profile()
        xml = (
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>"
            "<GetItemRequest xmlns=\"urn:ebay:apis:eBLBaseComponents\">"
            f"<ItemID>{item_id}</ItemID>"
            "<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>"
            "</GetItemRequest>"
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": profile.ebay_site_id,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": self.oauth.get_valid_token(),
            "Content-Type": "text/xml",
        }
        resp = self.session.post(
            "https://api.ebay.com/ws/api.dll",
            headers=headers,
            data=xml.encode("utf-8"),
            timeout=60,
        )
        body = resp.text
        ack_match = re.search(r"<Ack>(.*?)</Ack>", body, re.S)
        if ack_match and ack_match.group(1).strip() == "Failure":
            errors = [
                html.unescape((match.group(1) or "").strip())
                for match in re.finditer(r"<LongMessage>(.*?)</LongMessage>", body, re.S)
            ]
            raise RuntimeError(
                f"GetItem compatibility failed: {'; '.join(errors)[:400]}"
            )

        entries = []
        for block in re.findall(r"<Compatibility>(.*?)</Compatibility>", body, re.S):
            properties = []
            for name, value in re.findall(
                r"<Name>(.*?)</Name>\s*<Value>(.*?)</Value>", block, re.S
            ):
                properties.append(
                    {
                        "name": html.unescape(name).strip(),
                        "value": html.unescape(value).strip(),
                    }
                )
            if not properties:
                continue
            notes_match = re.search(r"<CompatibilityNotes>(.*?)</CompatibilityNotes>", block, re.S)
            entry = {"compatibilityProperties": properties}
            if notes_match:
                entry["notes"] = html.unescape(notes_match.group(1)).strip()
            entries.append(entry)
        return entries

    def revise_fixed_price_item_motors(self, item_id: str, *, description: Optional[str] = None,
                                       title: Optional[str] = None) -> Dict:
        """Partial-update a live Motors Trading item's description and/or title.

        On a Motors store, snapshot the live compatibility rows first and send
        them back with ``ReplaceAll=true``.  This fail-closed preservation gate
        prevents a description/title revision path from silently dropping a
        previously repaired fitment table.  Returns {"itemId", "ack"} or raises
        with eBay's error messages.
        """
        import re
        from src.services.motors_trading import build_revise_fixed_price_item_xml
        from src.utils.store_profile import get_store_profile
        profile = get_store_profile()
        compatibility = None
        replace_all_compatibility = False
        if profile.is_motors:
            compatibility = self.get_item_compatibility_motors(item_id)
            replace_all_compatibility = bool(compatibility)
        xml = build_revise_fixed_price_item_xml(
            item_id=item_id,
            description=description,
            title=title,
            compatibility=compatibility,
            replace_all_compatibility=replace_all_compatibility,
        )
        headers = {
            "X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem",
            "X-EBAY-API-SITEID": profile.ebay_site_id,
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1155",
            "X-EBAY-API-IAF-TOKEN": self.oauth.get_valid_token(),
            "Content-Type": "text/xml",
        }
        resp = self.session.post("https://api.ebay.com/ws/api.dll", headers=headers,
                                 data=xml.encode("utf-8"), timeout=90)
        body = resp.text
        ack = (re.search(r"<Ack>(.*?)</Ack>", body) or [None, ""])[1] if "<Ack>" in body else ""
        error_codes = set(re.findall(r"<ErrorCode>(.*?)</ErrorCode>", body, re.S))
        if (
            ack in ("Success", "Warning")
            and not error_codes.intersection(INVALID_MOTORS_COMPATIBILITY_ERROR_CODES)
        ):
            logging.info(f"[MOTORS] Revise OK: ItemID {item_id} (Ack={ack})")
            return {"itemId": item_id, "ack": ack}
        errors = [(m.group(1) or "").strip()
                  for m in re.finditer(r"<LongMessage>(.*?)</LongMessage>", body, re.S)]
        raise Exception(f"ReviseFixedPriceItem failed (Ack={ack}): {'; '.join(errors)[:400]}")

    def publish_offer(self, offer_id: str, max_retries: int = 3) -> Dict:
        """
        Publish offer to eBay

        POST /sell/inventory/v1/offer/{offerId}/publish
        
        Args:
            offer_id: Offer ID to publish
            max_retries: Maximum number of retry attempts
            
        Returns:
            {"listingId": "...", "status": "..."}
        """
        url = f"{self.base_url}/sell/inventory/v1/offer/{offer_id}/publish"
        
        for attempt in range(max_retries):
            try:
                token = self.oauth.get_valid_token()
                
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Content-Language": "en-US"
                }
                
                # No payload needed - Country comes from merchantLocationKey in offer
                response = self.session.post(url, headers=headers, timeout=120)
                
                if response.status_code == 200:
                    result = response.json()
                    listing_id = result.get("listingId")
                    print(f"[OK] Listing published: {listing_id}")
                    return result
                elif response.status_code == 400:
                    # Bad request - parse error details for specific message
                    error_text = response.text
                    logging.error(f"[ERROR] Publish failed (400): {error_text[:500]}")
                    
                    # Try to extract the actual error message from eBay response
                    try:
                        error_data = response.json()
                        errors = error_data.get("errors", [])
                        if errors:
                            # Build detailed error from all eBay error messages
                            error_msgs = []
                            for err in errors:
                                msg = err.get("message", "")
                                if msg:
                                    error_msgs.append(msg)
                            if error_msgs:
                                raise Exception(f"Publish failed: {'; '.join(error_msgs)}")
                    except (ValueError, KeyError):
                        pass
                    
                    raise Exception(f"Publish failed: {error_text[:300]}")
                else:
                    logging.error(f"[ERROR] Publish failed: {response.status_code}")
                    logging.error(f"[ERROR] Response: {response.text[:500]}")
                    
                    # Retry on connection/server errors
                    if response.status_code >= 500 or response.status_code == 0:
                        if attempt < max_retries - 1:
                            wait_time = (attempt + 1) * 2
                            print(f"[WARN] Server error, retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                            time.sleep(wait_time)
                            continue
                    
                    response.raise_for_status()
                    
            except requests.exceptions.ConnectionError as e:
                logging.error(f"[ERROR] Connection error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 3
                    print(f"[WARN] Connection error, retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"Connection failed after {max_retries} attempts: {e}")
            except requests.exceptions.Timeout as e:
                logging.error(f"[ERROR] Timeout on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 3
                    print(f"[WARN] Timeout, retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
                else:
                    raise Exception(f"Request timed out after {max_retries} attempts")
        
        raise Exception(f"Publish failed after {max_retries} attempts")
    
    @staticmethod
    def _positive_offer_quantity(value, default: int = 1) -> int:
        try:
            quantity = int(value)
        except (TypeError, ValueError):
            quantity = default
        return max(default, quantity)

    def update_offer_category(self, offer_id: str, category_id: str, price: float = None, listing_description: str = None) -> bool:
        """
        Update offer with correct category ID and optionally listing description
        
        PUT /sell/inventory/v1/offer/{offerId}
        
        Args:
            offer_id: Existing offer ID
            category_id: New category ID
            price: Offer price (optional)
            listing_description: HTML description for the live listing (optional)
            
        Returns:
            True if successful
        """
        # First, get current offer details
        get_url = f"{self.base_url}/sell/inventory/v1/offer/{offer_id}"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        try:
            response = self.session.get(get_url, headers=headers, timeout=60)
            if response.status_code != 200:
                logging.error(f"Failed to get offer {offer_id}: {response.status_code}")
                return False
            
            offer_data = response.json()
            
            # 获取并清理 listingDescription（避免长度超限问题）
            if listing_description:
                # Use the provided description (from AI optimization)
                listing_desc = listing_description
            else:
                listing_desc = offer_data.get("listingDescription", "")
            
            if not listing_desc or len(listing_desc) < 50:
                # 如果描述为空或太短，使用基础描述
                listing_desc = "Brand new product. Please refer to product images for details."
            elif len(listing_desc) > 50000:  # 智能截断HTML，保持结构完整
                from src.utils.html_truncator import smart_truncate_html
                listing_desc = smart_truncate_html(listing_desc, max_length=50000, min_length=45000)
            
            put_headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Content-Language": "en-US",
                "Accept": "application/json"
            }

            def _build_update_payload(quantity: int) -> Dict[str, Any]:
                payload = {
                    "availableQuantity": self._positive_offer_quantity(quantity),
                    "categoryId": category_id,
                    "listingDescription": listing_desc,
                    "listingDuration": offer_data.get("listingDuration", "GTC"),
                    "listingPolicies": self._complete_listing_policies(offer_data.get("listingPolicies", {})),
                    "merchantLocationKey": offer_data.get("merchantLocationKey", get_store_profile().merchant_location_key),
                    "pricingSummary": offer_data.get("pricingSummary", {}),
                }
                if price:
                    payload["pricingSummary"] = {
                        "price": {
                            "value": str(price),
                            "currency": "USD"
                        }
                    }
                return payload

            quantity = self._positive_offer_quantity(offer_data.get("availableQuantity", 1))
            response = self.session.put(
                get_url,
                headers=put_headers,
                json=_build_update_payload(quantity),
                timeout=120,
            )

            if response.status_code in [200, 204]:
                print(f"[OK] Offer {offer_id} updated with category: {category_id}")
                return True

            error_text = response.text[:500]
            if quantity != 1 and any(
                marker in error_text.lower()
                for marker in ("selling limit", "amount you can list", "exceed the amount you can list")
            ):
                logging.warning(
                    f"Offer {offer_id} update hit selling limit at quantity {quantity}; retrying with quantity=1"
                )
                response = self.session.put(
                    get_url,
                    headers=put_headers,
                    json=_build_update_payload(1),
                    timeout=120,
                )
                if response.status_code in [200, 204]:
                    print(f"[OK] Offer {offer_id} updated with category: {category_id} (quantity fallback=1)")
                    return True
                error_text = response.text[:500]

            verified_offer = self.get_offer(offer_id) or {}
            verified_category = str(
                verified_offer.get("categoryId")
                or (verified_offer.get("category") or {}).get("categoryId")
                or ""
            ).strip()
            verified_description = _normalize_offer_description_for_compare(
                verified_offer.get("listingDescription", "")
            )
            expected_description = _normalize_offer_description_for_compare(listing_desc)

            if verified_category == str(category_id).strip() and (
                not listing_desc or verified_description == expected_description
            ):
                logging.warning(
                    "Offer %s PUT returned HTTP %s, but follow-up GET matches target category/description; "
                    "treating as success",
                    offer_id,
                    response.status_code,
                )
                return True

            logging.error(f"Failed to update offer: {response.status_code} - {error_text[:200]}")
            return False
        
        except Exception as e:
            logging.error(f"Error updating offer: {e}")
            return False
            
    def delete_offer(self, offer_id: str) -> bool:
        """
        Delete an offer
        
        DELETE /sell/inventory/v1/offer/{offerId}
        """
        url = f"{self.base_url}/sell/inventory/v1/offer/{offer_id}"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}"
        }
        
        try:
            response = self.session.delete(url, headers=headers)
            if response.status_code in [200, 204]:
                logging.info(f"[SUCCESS] Offer deleted: {offer_id}")
                return True
            else:
                logging.error(f"Failed to delete offer: {response.status_code}")
                return False
        except Exception as e:
            logging.error(f"Error deleting offer: {e}")
            return False

    def get_offers_by_sku(self, sku: str, marketplace_id: Optional[str] = None) -> list:
        """
        Get all offers for a specific SKU.
        
        GET /sell/inventory/v1/offer?sku={sku}
        
        Returns:
            List of offer dicts, or empty list on error
        """
        url = f"{self.base_url}/sell/inventory/v1/offer"
        token = self.oauth.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {"sku": sku}
        if marketplace_id:
            params["marketplace_id"] = marketplace_id
        
        try:
            response = self.session.get(url, headers=headers, params=params)
            if response.status_code == 200:
                data = response.json()
                offers = data.get("offers", [])

                def _sort_key(offer: Dict[str, Any]):
                    listing = offer.get("listing") or {}
                    listing_status = listing.get("listingStatus", "")
                    status = offer.get("status", "")
                    offer_marketplace = offer.get("marketplaceId", "")
                    marketplace_rank = 0 if marketplace_id and offer_marketplace == marketplace_id else 1
                    active_rank = 0 if listing_status == "ACTIVE" else 1
                    published_rank = 0 if status == "PUBLISHED" else 1
                    return (marketplace_rank, active_rank, published_rank, offer.get("offerId", ""))

                return sorted(offers, key=_sort_key)
            elif response.status_code == 404:
                return []
            else:
                logging.warning(f"get_offers_by_sku({sku}): HTTP {response.status_code}")
                return []
        except Exception as e:
            logging.error(f"get_offers_by_sku error: {e}")
            return []

    def withdraw_offer(self, offer_id: str) -> bool:
        """
        Withdraw (end) a published offer, ending the live eBay listing.
        
        POST /sell/inventory/v1/offer/{offerId}/withdraw
        
        Returns:
            True if successful
        """
        url = f"{self.base_url}/sell/inventory/v1/offer/{offer_id}/withdraw"
        token = self.oauth.get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        
        try:
            response = self.session.post(url, headers=headers)
            if response.status_code in [200, 204]:
                logging.info(f"[SUCCESS] Offer withdrawn: {offer_id}")
                return True
            else:
                logging.error(f"Failed to withdraw offer {offer_id}: {response.status_code} - {response.text[:200]}")
                return False
        except Exception as e:
            logging.error(f"Error withdrawing offer: {e}")
            return False

    def delete_inventory_item(self, sku: str) -> bool:
        """
        Delete an inventory item by SKU.
        
        DELETE /sell/inventory/v1/inventory_item/{sku}
        
        Returns:
            True if successful
        """
        url = f"{self.base_url}/sell/inventory/v1/inventory_item/{sku}"
        token = self.oauth.get_valid_token()
        headers = {"Authorization": f"Bearer {token}"}
        
        try:
            response = self.session.delete(url, headers=headers)
            if response.status_code in [200, 204]:
                logging.info(f"[SUCCESS] Inventory item deleted: {sku}")
                return True
            else:
                logging.error(f"Failed to delete inventory {sku}: {response.status_code}")
                return False
        except Exception as e:
            logging.error(f"Error deleting inventory item: {e}")
            return False

    def delist_sku(self, sku: str) -> dict:
        """
        Full delist flow: withdraw all offers → delete offers → delete inventory item.
        
        Returns:
            {'success': bool, 'steps': [...], 'error': str|None}
        """
        steps = []
        
        # 1. Get all offers for this SKU
        offers = self.get_offers_by_sku(sku)
        steps.append(f"Found {len(offers)} offer(s) for {sku}")
        
        # 2. Withdraw and delete each offer
        for offer in offers:
            offer_id = offer.get("offerId")
            status = offer.get("status", "")
            
            if status == "PUBLISHED":
                ok = self.withdraw_offer(offer_id)
                steps.append(f"Withdraw offer {offer_id}: {'OK' if ok else 'FAILED'}")
                if not ok:
                    return {"success": False, "steps": steps, "error": f"Failed to withdraw offer {offer_id}"}
            
            ok = self.delete_offer(offer_id)
            steps.append(f"Delete offer {offer_id}: {'OK' if ok else 'FAILED'}")
            if not ok:
                return {
                    "success": False,
                    "steps": steps,
                    "error": f"Failed to delete offer {offer_id}",
                }
        
        # 3. Delete inventory item
        ok = self.delete_inventory_item(sku)
        steps.append(f"Delete inventory item {sku}: {'OK' if ok else 'FAILED'}")
        if not ok:
            return {
                "success": False,
                "steps": steps,
                "error": f"Failed to delete inventory item {sku}",
            }
        
        return {"success": True, "steps": steps, "error": None}

    def upload_video(self, video_url: str, title: str, description: str = "") -> str:
        """
        Upload video to eBay Media API
        
        POST /sell/media/v1/video
        
        Args:
            video_url: URL of video to upload
            title: Video title
            description: Video description
            
        Returns:
            video_id: eBay video ID
        """
        url = f"{self.base_url}/sell/media/v1/video"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "title": title,
            "description": description or title,
            "videoUrl": video_url
        }
        
        response = self.session.post(url, headers=headers, json=payload)
        response.raise_for_status()
        
        result = response.json()
        video_id = result.get("videoId")
        
        video_id = result.get("videoId")
        
        print(f"[OK] Video upload initiated: {video_id}")
        
        return video_id
    
    def get_video_status(self, video_id: str) -> Dict:
        """
        Get video processing status
        
        GET /sell/media/v1/video/{videoId}
        
        Args:
            video_id: eBay video ID
            
        Returns:
            {"status": "PENDING|PROCESSING|LIVE|FAILED", ...}
        """
        url = f"{self.base_url}/sell/media/v1/video/{video_id}"
        
        token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        response = self.session.get(url, headers=headers)
        response.raise_for_status()
        
        return response.json()
    
    def get_category_suggestions(self, title: str, limit: int = 3) -> List[Dict]:
        """
        Get category suggestions based on title using Taxonomy API
        
        GET /commerce/taxonomy/v1/category_tree/{category_tree_id}/get_category_suggestions
        
        Args:
            title: Product title
            limit: Max number of suggestions
            
        Returns:
            List of category suggestions
        """
        # EBAY_US category tree ID
        category_tree_id = get_store_profile().category_tree_id
        url = f"{self.base_url}/commerce/taxonomy/v1/category_tree/{category_tree_id}/get_category_suggestions"
        
        # Use Application Token for Taxonomy API (public API, doesn't need user auth)
        try:
            token = self.oauth.get_application_token()
        except Exception as e:
            logging.warning(f"[WARN] Failed to get application token: {e}, using user token")
            token = self.oauth.get_valid_token()
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        
        params = {
            "q": title[:100]  # Limit query length
        }
        
        try:
            response = self.session.get(url, headers=headers, params=params)
            
            if response.status_code == 403:
                # Fallback: Return common category mappings
                logging.warning("[WARN] Taxonomy API 403, using fallback categories")
                return self._get_fallback_category(title)
            
            response.raise_for_status()
            data = response.json()
            return data.get("categorySuggestions", [])[:limit]
            
        except Exception as e:
            logging.error(f"[ERROR] Category suggestion failed: {e}")
            return self._get_fallback_category(title)
    
    def _get_fallback_category(self, title: str) -> List[Dict]:
        """
        Return fallback category based on keywords in title
        """
        title_lower = title.lower()
        
        # Common product -> category mappings
        category_map = {
            # Pet supplies
            "dog crate": {"categoryId": "121851", "categoryName": "Dog Cages & Crates"},
            "dog kennel": {"categoryId": "121851", "categoryName": "Dog Cages & Crates"},
            "dog bed": {"categoryId": "20744", "categoryName": "Beds"},
            "cat litter": {"categoryId": "100411", "categoryName": "Litter Boxes"},
            "litter box": {"categoryId": "100411", "categoryName": "Litter Boxes"},
            "cat tree": {"categoryId": "20740", "categoryName": "Furniture & Scratchers"},
            "cat cabinet": {"categoryId": "20740", "categoryName": "Furniture & Scratchers"},
            "cat house": {"categoryId": "20740", "categoryName": "Furniture & Scratchers"},
            "pet bed": {"categoryId": "20744", "categoryName": "Beds"},
            "chicken coop": {"categoryId": "63108", "categoryName": "Cages, Hutches & Enclosure"},
            "garden statue": {"categoryId": "29511", "categoryName": "Ornaments & Statues"},
            "statue": {"categoryId": "29511", "categoryName": "Ornaments & Statues"},
            "planter": {"categoryId": "20518", "categoryName": "Baskets, Pots, Window Boxes & Saucers"},
            
            # Furniture - Living Room
            "sofa": {"categoryId": "38208", "categoryName": "Sofas, Armchairs & Couches"},
            "sectional": {"categoryId": "38208", "categoryName": "Sofas, Armchairs & Couches"},
            "couch": {"categoryId": "38208", "categoryName": "Sofas, Armchairs & Couches"},
            "loveseat": {"categoryId": "38208", "categoryName": "Sofas, Armchairs & Couches"},
            
            # Furniture - Bedroom
            "bed": {"categoryId": "131604", "categoryName": "Beds & Bedframes"},
            "race car bed": {"categoryId": "131604", "categoryName": "Beds & Bedframes"},
            "platform bed": {"categoryId": "131604", "categoryName": "Beds & Bedframes"},
            "bunk bed": {"categoryId": "131604", "categoryName": "Beds & Bedframes"},
            
            # Furniture - Other
            "desk": {"categoryId": "88057", "categoryName": "Desks & Tables"},
            "vanity": {"categoryId": "32878", "categoryName": "Vanities & Makeup Tables"},
            "tv stand": {"categoryId": "20488", "categoryName": "TV Stands & Entertainment Units"},
            "coffee table": {"categoryId": "38204", "categoryName": "Coffee Tables"},
            "dining table": {"categoryId": "38204", "categoryName": "Tables"},
            "bookshelf": {"categoryId": "3199", "categoryName": "Bookcases"},
            "cabinet": {"categoryId": "38221", "categoryName": "Cabinets & Cupboards"},
            "chair": {"categoryId": "54235", "categoryName": "Chairs"},
            
            # Office
            "office chair": {"categoryId": "54235", "categoryName": "Office Chairs"},
            
            # Outdoor
            "patio": {"categoryId": "139849", "categoryName": "Patio & Garden Furniture Sets"},
            "outdoor": {"categoryId": "139849", "categoryName": "Patio & Garden Furniture Sets"},
        }
        
        # Find matching category
        for keyword, category in category_map.items():
            if keyword in title_lower:
                return [{"category": category}]
        
        # Default to Home & Garden -> Furniture
        return [{"category": {"categoryId": "3197", "categoryName": "Furniture"}}]
        response.raise_for_status()
        
        result = response.json()
        suggestions = result.get("categorySuggestions", [])
        
        return suggestions[:limit]


    def upload_picture(self, image_url: str) -> Optional[str]:
        """
        Upload a picture to eBay's Picture Services (EPS) via UploadSiteHostedPictures.
        
        Downloads the image from the source URL and uploads it to eBay,
        returning a permanent eBay-hosted URL (https://i.ebayimg.com/...).
        
        Args:
            image_url: Source image URL (e.g., GigaCloud CDN)
            
        Returns:
            eBay-hosted image URL, or None on failure
        """
        try:
            # 1. Download the image
            resp = requests.get(image_url, timeout=30, stream=True, verify=False)
            if resp.status_code != 200:
                logging.warning(f"[EPS] Failed to download image: HTTP {resp.status_code} from {image_url[:80]}")
                return None
            
            image_data = resp.content
            content_type = resp.headers.get('Content-Type', 'image/jpeg')
            
            # Validate it's actually an image
            if not content_type.startswith('image/'):
                logging.warning(f"[EPS] Not an image: {content_type} from {image_url[:80]}")
                return None
            
            try:
                image_data, content_type, (width, height) = normalize_eps_image_data(
                    image_data,
                    content_type,
                )
            except Exception as image_exc:
                logging.warning(f"[EPS] Failed to prepare image for EPS: {image_exc}")
                return None

            if len(image_data) > EPS_MAX_IMAGE_BYTES:
                logging.warning(
                    f"[EPS] Image too large after normalization: {len(image_data)} bytes from {image_url[:80]}"
                )
                return None

            if width < EPS_MIN_IMAGE_SIDE or height < EPS_MIN_IMAGE_SIDE:
                logging.warning(
                    f"[EPS] Image too small after normalization ({width}x{height}) from {image_url[:80]}"
                )
                return None
            
            # 2. Build XML request
            xml_request = """<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <ErrorLanguage>en_US</ErrorLanguage>
    <WarningLevel>High</WarningLevel>
    <PictureName>product_image</PictureName>
    <PictureSet>Supersize</PictureSet>
</UploadSiteHostedPicturesRequest>"""
            
            # 3. Get OAuth token
            token = self.oauth.get_valid_token()
            
            # 4. Build Trading API headers
            app_id = os.environ.get('EBAY_APP_ID', '')
            dev_id = os.environ.get('EBAY_DEV_ID', '')
            cert_id = os.environ.get('EBAY_CERT_ID', '')
            
            env = self.oauth.environment.lower()
            api_endpoint = "https://api.ebay.com/ws/api.dll" if env == "production" else "https://api.sandbox.ebay.com/ws/api.dll"
            
            # 5. Build multipart/form-data with XML + binary image
            boundary = "MIME_boundary_eBay_image_upload"
            body = (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"XML Payload\"\r\n"
                f"Content-Type: text/xml;charset=utf-8\r\n\r\n"
                f"{xml_request}\r\n"
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"image\"; filename=\"image.jpg\"\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Transfer-Encoding: binary\r\n\r\n"
            ).encode('utf-8') + image_data + f"\r\n--{boundary}--\r\n".encode('utf-8')
            
            headers = {
                "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
                "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
                "X-EBAY-API-SITEID": "0",
                "X-EBAY-API-IAF-TOKEN": token,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            }
            
            # 6. Upload
            resp = requests.post(api_endpoint, headers=headers, data=body, timeout=60, verify=False)
            
            if resp.status_code != 200:
                logging.error(f"[EPS] Upload failed: HTTP {resp.status_code}")
                return None
            
            # 7. Parse response XML for the hosted URL
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.text)
            ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
            
            ack = root.find('ebay:Ack', ns)
            if ack is not None and ack.text in ('Success', 'Warning'):
                pic_data = root.find('.//ebay:SiteHostedPictureDetails', ns)
                if pic_data is not None:
                    full_url = pic_data.find('ebay:FullURL', ns)
                    if full_url is not None and full_url.text:
                        logging.info(f"[EPS] Uploaded: {full_url.text[:80]}")
                        return full_url.text
            
            # Parse errors
            errors = root.findall('.//ebay:Errors', ns)
            for err in errors:
                code = err.find('ebay:ErrorCode', ns)
                msg = err.find('ebay:LongMessage', ns)
                logging.error(f"[EPS] Error {code.text if code is not None else '?'}: {msg.text if msg is not None else '?'}")
            
            return None
            
        except Exception as e:
            logging.error(f"[EPS] Exception uploading image: {e}")
            return None
    
    def upload_images_to_eps(self, image_urls: List[str], max_images: int = 24) -> List[str]:
        """
        Upload multiple images to eBay EPS, returning eBay-hosted URLs.
        Skips failed uploads to avoid mixed EPS/non-EPS URL errors.

        Args:
            image_urls: List of source image URLs
            max_images: Maximum number of images to upload (eBay limit used by this app: 24)

        Returns:
            List of eBay-hosted image URLs (only successfully uploaded)
        """
        eps_urls = []
        for i, url in enumerate(image_urls[:max_images]):
            clean_url = clean_image_url(url) if url else ""
            if not clean_url:
                continue

            # Try to upload to EPS with retry for SSL errors
            eps_url = None
            for attempt in range(3):
                eps_url = self.upload_picture(url)
                if not eps_url:
                    eps_url = self.upload_picture(clean_url)
                if eps_url:
                    break
                logging.warning(f"[EPS] Upload attempt {attempt+1}/3 failed for image {i+1}, retrying in {3*(attempt+1)}s...")
                time.sleep(3 * (attempt + 1))

            if eps_url:
                eps_urls.append(eps_url)
            else:
                # Do NOT fall back to non-EPS URL — eBay rejects mixed sets
                logging.warning(f"[EPS] Upload failed for image {i+1} after 3 attempts, skipping (no fallback)")

            # Rate limit
            if i < len(image_urls[:max_images]) - 1:
                time.sleep(0.5)

        return eps_urls


# Factory function for easy initialization
def create_real_ebay_client(environment: str = "SANDBOX") -> RealEbayClient:
    """
    Create a real eBay client with all dependencies
    
    Args:
        environment: "SANDBOX" or "PRODUCTION"
        
    Returns:
        Configured RealEbayClient instance
    """
    oauth = EbayOAuthService(environment)
    policy_manager = EbayPolicyManager(oauth)
    
    return RealEbayClient(oauth, policy_manager)


if __name__ == "__main__":
    # Test real eBay client
    client = create_real_ebay_client()
    
    if not client.oauth.is_authorized():
        print("[!] Not authorized. Please run OAuth flow first.")
        print(f"Authorization URL: {client.oauth.get_authorization_url()}")
        exit(1)
    
    print("[OK] eBay client initialized and authorized")
    
    # Test category suggestions
    suggestions = client.get_category_suggestions("Dining Chair")
    print(f"\n📂 Category Suggestions for 'Dining Chair':")
    for s in suggestions:
        print(f"  - {s.get('category', {}).get('categoryName')} (ID: {s.get('category', {}).get('categoryId')})")
