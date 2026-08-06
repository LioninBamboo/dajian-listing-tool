#!/usr/bin/env python3
"""
Robust Batch Publisher — Publish all READY products to eBay

Features:
  1. Dajian API dimension enrichment (auto-fetch missing L/W/H/Weight)
  2. Terapeak market-aware smart pricing
  3. Image upload to eBay EPS (permanent hosting)
  4. Auto category matching + required aspect completion
  5. Price validation against cost + margin
  6. Robust error handling with retries & auto-fix
  7. Detailed logging

Usage:
  python batch_publish.py                  # Publish all READY
  python batch_publish.py --dry-run        # Preview without publishing
  python batch_publish.py --sku LP000138AAE  # Publish single SKU
  python batch_publish.py --enrich-only    # Only enrich dimensions + pricing, no publish
"""
import os
import sys
import json
import re
import time
import argparse
import sqlite3
import logging
import traceback
import requests
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from sqlalchemy.orm.attributes import flag_modified
from src.utils.html_truncator import smart_truncate_html
from src.utils.title_sanitizer import (
    normalize_listing_title_for_ebay,
    strip_supplier_brand_prefix,
)
from src.utils.publish_validation import (
    MEASUREMENT_ASPECT_KEYS,
    REQUIRED_MEASUREMENT_ASPECT_KEYS,
    category_validation_errors as _category_validation_errors,
    first_aspect_text as _first_aspect_text,
    measurement_issue as _measurement_issue,
    measurement_validation_errors as _measurement_validation_errors,
)
from src.utils.listing_quality_gate import (
    normalize_generated_listing as _normalize_generated_listing,
)
from src.services.listing_qc import run_listing_qc
from src.utils.publish_autofix import (
    is_invalid_category_error,
    sanitize_placeholder_aspects,
    sanitize_single_value_aspects,
    try_fix_publish_error,
)
from src.utils.publish_aspect_completion import complete_publish_aspects
from src.services.taxonomy_constants import INVALID_CATEGORY_REMAP, PROTECTED_STORED_CATEGORY_IDS
from src.utils.dimension_helpers import (
    extract_all_dimensions, find_dimension,
    build_package_weight_and_size, extract_product_weight_from_text,
    extract_dajian_measurements,
    replace_description_weight_placeholder_with_package_weight,
)
from src.utils.ebay_quantity import resolve_publish_quantity
from src.services.ebay_publisher import EbayPublisher
from src.services.pricing_engine import PricingEngine
from src.services.vehicle_compatibility import (
    EBAY_MOTORS_CATEGORIES,
    analyze_ebay_motors_compatibility,
    apply_compatibility_aspects,
    serialize_compatibility_analysis,
)
from src.services.listing_publish_readback import (
    build_publish_readback_expectation,
    verify_publish_readback,
)
from src.clients.real_ebay_client import PRODUCT_IDENTIFIER_UNAVAILABLE_TEXT
# ─── Logging ───────────────────────────────────────────────────
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"batch_publish_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────
from src.utils.store_profile import get_store_profile

BRAND_NAME = get_store_profile().brand_name
MAX_RETRIES = 3
PUBLISH_DELAY_SECS = 2.0    # delay between products


def _publish_image_limit(sku: str) -> int:
    """Return an explicit per-run image cap, bounded by eBay's 24-image limit."""
    raw = os.getenv(f"PUBLISH_IMAGE_LIMIT_{sku}") or os.getenv("PUBLISH_IMAGE_LIMIT")
    if raw is None:
        return 24
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 24
    return value if 1 <= value <= 24 else 24

_CATEGORY_VALIDITY_CACHE = {}
KNOWN_PUBLISHABLE_CATEGORY_IDS = {
    "38217",  # China Cabinets
    "20493",  # Display Cabinets
    "79694",  # Porch Swings
    "38200",  # End Tables
    "262980", # Benches
    "20488",  # TV Stands
    "63108",  # Chicken Coops
    "88057",  # Desks & Tables
    "175758", # Beds & Bed Frames
}
# ═══════════════════════════════════════════════════════════════
# Data Layer
# ═══════════════════════════════════════════════════════════════

def _normalize_sku_filters(sku_filters) -> list[str]:
    out = []
    seen = set()
    for raw in sku_filters or []:
        sku = str(raw or '').strip()
        if not sku or sku in seen:
            continue
        seen.add(sku)
        out.append(sku)
    return out


def get_ready_products(sku_filter: str = None, sku_filters=None) -> list:
    """Load all READY products from SQLite."""
    conn = sqlite3.connect(str(PROJECT_ROOT / "ebay_collection.db"), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    sku_list = _normalize_sku_filters(sku_filters)

    if sku_filter and sku_list:
        conn.close()
        raise ValueError("Use either sku_filter or sku_filters, not both")

    if sku_list:
        placeholders = ",".join("?" for _ in sku_list)
        rows = conn.execute(
            "SELECT * FROM collected_products "
            "WHERE status IN ('READY','READY_TO_PUBLISH') "
            f"AND sku IN ({placeholders})",
            sku_list,
        ).fetchall()
    elif sku_filter:
        rows = conn.execute(
            "SELECT * FROM collected_products WHERE status IN ('READY','READY_TO_PUBLISH') AND sku=?",
            (sku_filter,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM collected_products WHERE status IN ('READY','READY_TO_PUBLISH') ORDER BY sku"
        ).fetchall()

    products_by_sku = {}
    products = []
    for r in rows:
        d = dict(r)
        for field in ('optimization', 'cost_breakdown', 'images', 'videos', 'specs', 'attributes', 'logs'):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        if sku_list:
            products_by_sku[d.get('sku')] = d
        else:
            products.append(d)

    if sku_list:
        products = [products_by_sku[sku] for sku in sku_list if sku in products_by_sku]

    conn.close()
    return products


def update_product_status(sku: str, listing_id: str, offer_id: str = None,
                          category_id: str = None, extra_logs: list = None):
    """Mark product as PUBLISHED in the database."""
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct

    db = SessionLocal()
    try:
        product = db.query(CollectedProduct).filter_by(sku=sku).first()
        if not product:
            return
        product.status = "PUBLISHED"
        product.listing_id = listing_id
        product.published_at = datetime.now(timezone.utc)
        log_entries = product.logs or []
        log_entries.append(f"Published at {datetime.now(timezone.utc).isoformat()}")
        log_entries.append(f"Listing: {listing_id}, Offer: {offer_id}, Cat: {category_id}")
        if extra_logs:
            log_entries.extend(extra_logs)
        product.logs = log_entries
        flag_modified(product, 'logs')
        db.commit()
    finally:
        db.close()


def record_error(sku: str, error_msg: str):
    """Append error to product logs without changing status."""
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct

    db = SessionLocal()
    try:
        product = db.query(CollectedProduct).filter_by(sku=sku).first()
        if not product:
            return
        log_entries = product.logs or []
        log_entries.append(f"[ERROR {datetime.now(timezone.utc).isoformat()}] {error_msg[:300]}")
        product.logs = log_entries
        flag_modified(product, 'logs')
        db.commit()
    finally:
        db.close()


def _persist_prepared_listing(
    sku: str,
    category_id: str,
    category_name: str,
    title: str,
    description: str,
    aspects: dict,
    compatibility=None,
):
    """Persist prepared title/description/category/aspects into the local optimization record."""
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct

    db = SessionLocal()
    try:
        product = db.query(CollectedProduct).filter_by(sku=sku).first()
        if not product:
            return

        optimization = dict(product.optimization or {})
        optimization["categoryId"] = category_id
        if category_name:
            optimization["categoryName"] = category_name
        optimization["title"] = title
        optimization["description"] = description
        optimization["aspects"] = aspects
        if compatibility is not None:
            optimization["motorsCompatibility"] = serialize_compatibility_analysis(compatibility)
        product.optimization = optimization
        flag_modified(product, "optimization")

        log_entries = product.logs or []
        if compatibility is not None:
            compat_summary = f"[COMPAT {datetime.now(timezone.utc).isoformat()}] {compatibility.summary}"
            if compat_summary not in log_entries:
                log_entries.append(compat_summary)
            for issue in compatibility.issues:
                msg = f"[COMPAT-WARN {datetime.now(timezone.utc).isoformat()}] {issue}"
                if msg not in log_entries:
                    log_entries.append(msg)
        product.logs = log_entries
        flag_modified(product, "logs")
        db.commit()
    except Exception as e:
        logger.warning(f"  [{sku}] Failed to persist prepared listing metadata: {e}")
        db.rollback()
    finally:
        db.close()


def _sync_motors_compatibility(ebay_client, sku: str, compatibility) -> None:
    """Apply or clear eBay Motors compatibility for the inventory item."""
    if compatibility.mode == "not_applicable":
        return

    if compatibility.compatible_products:
        ebay_client.create_or_replace_product_compatibility(sku, compatibility.compatible_products)
        return

    ebay_client.delete_product_compatibility(sku)


def _target_marketplace(category_id: str):
    """Return the correct marketplace for the target category."""
    return "EBAY_MOTORS" if category_id in EBAY_MOTORS_CATEGORIES else None


def _is_invalid_category_error(error_msg: str) -> bool:
    """Match eBay category validation errors robustly."""
    return is_invalid_category_error(error_msg)


def _is_sellable_leaf_category(oauth, category_id: str) -> bool:
    """Validate category ID against EBAY_US taxonomy tree 0 and ensure it's a leaf."""
    cid = str(category_id or "").strip()
    if not cid:
        return False
    if cid in KNOWN_PUBLISHABLE_CATEGORY_IDS:
        _CATEGORY_VALIDITY_CACHE[cid] = True
        return True
    if cid in _CATEGORY_VALIDITY_CACHE:
        return _CATEGORY_VALIDITY_CACHE[cid]

    try:
        token = oauth.get_application_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        # Validate against THIS instance's tree: Motors ids (33651 Roof Racks,
        # 33653 Trailer Hitches, ...) exist only in tree 100 and 400 in tree 0.
        resp = requests.get(
            f"{oauth.api_base}/commerce/taxonomy/v1/category_tree/"
            f"{get_store_profile().category_tree_id}/get_category_subtree",
            headers=headers,
            params={"category_id": cid},
            timeout=30,
        )
        valid = False
        if resp.status_code == 200:
            node = (resp.json() or {}).get("categorySubtreeNode", {})
            valid = bool(node.get("leafCategoryTreeNode"))
        _CATEGORY_VALIDITY_CACHE[cid] = valid
        return valid
    except Exception:
        _CATEGORY_VALIDITY_CACHE[cid] = False
        return False


def _is_invalid_compatibility_error(error_msg: str) -> bool:
    """Match eBay Motors structured compatibility validation failures."""
    normalized = (error_msg or "").lower()
    markers = (
        "all compatibilities are invalid",
        "compatibilities are invalid",
        "invalid compatibility",
    )
    return any(marker in normalized for marker in markers)


def _delete_stale_offers(ebay_client, sku: str) -> int:
    """Delete unpublished offers so the next retry can recreate them cleanly."""
    deleted = 0
    for offer in ebay_client.get_offers_by_sku(sku):
        offer_id = offer.get("offerId")
        listing_status = (offer.get("listing") or {}).get("listingStatus", "")
        if not offer_id or listing_status == "ACTIVE":
            continue
        if ebay_client.delete_offer(offer_id):
            deleted += 1
    return deleted


# ═══════════════════════════════════════════════════════════════
# Dajian Dimension Enrichment
# ═══════════════════════════════════════════════════════════════

def fetch_dajian_dimensions(sku: str) -> dict:
    """Fetch product dimensions & weight from Dajian API.
    
    Returns dict with keys:
      - length/width/height/packageWeight for shipping/package data
      - assembledLength/Width/Height/productWeight for product display data
    """
    try:
        from src.clients.dajian_client import DaJianClient
        client_id = os.getenv("DAJIAN_API_KEY")
        client_secret = os.getenv("DAJIAN_API_SECRET")
        if not client_id or not client_secret:
            logger.warning(f"  [{sku}] Dajian API credentials not configured")
            return {}
        dajian = DaJianClient(client_id, client_secret)
        detail = dajian.get_product_detail_by_sku(sku)
        if not detail:
            logger.warning(f"  [{sku}] No Dajian detail data")
            return {}
        dims = extract_dajian_measurements(detail)
        logger.info(f"  [{sku}] Dajian dims: L={dims.get('length')} W={dims.get('width')} "
                    f"H={dims.get('height')} PkgWt={dims.get('packageWeight')}lb "
                    f"ProdWt={dims.get('productWeight')}lb "
                    f"Asm=({dims.get('assembledLength')},{dims.get('assembledWidth')},{dims.get('assembledHeight')})")
        return dims
    except Exception as e:
        logger.warning(f"  [{sku}] Dajian dims fetch failed: {e}")
        return {}


def enrich_product_dimensions(product: dict) -> dict:
    """Enrich product attributes/specs with Dajian API dimensions.
    
    Fetches from Dajian API if key dimensions are missing, then persists to DB.
    Returns the dajian_dims dict (may be empty).
    """
    sku = product['sku']
    attrs = dict(product.get('attributes') or {})
    specs = dict(product.get('specs') or {})
    desc_weight = extract_product_weight_from_text(product.get('description', ''))
    current_product_weight = attrs.get('Product Weight (lbs.)')
    package_weight_raw = specs.get('Package Weight (lbs.)')
    current_description = product.get('description', '') or ''
    suspect_product_weight = False
    try:
        if current_product_weight and package_weight_raw and desc_weight:
            suspect_product_weight = (
                str(current_product_weight).strip() == str(package_weight_raw).strip()
                and abs(float(desc_weight) - float(package_weight_raw)) > 0.5
            )
    except (TypeError, ValueError):
        suspect_product_weight = False
    
    # Check which dimensions are missing
    missing_keys = [k for k in ['Assembled Length (in.)', 'Assembled Width (in.)',
                                 'Assembled Height (in.)', 'Product Weight (lbs.)']
                    if k not in attrs or not attrs.get(k)]
    if suspect_product_weight and 'Product Weight (lbs.)' not in missing_keys:
        missing_keys.append('Product Weight (lbs.)')

    missing_description_context = not current_description.strip()

    if not missing_keys and not missing_description_context:
        logger.info(f"  [{sku}] Dimensions and description context already present")
        return {}  # all good

    logger.info(f"  [{sku}] Missing: {missing_keys or ['description context']} — fetching from Dajian API...")
    dajian_dims = fetch_dajian_dimensions(sku)
    
    if not dajian_dims:
        return {}
    
    changed = False
    # Package dims → specs
    for k, s in [('length', 'Package Length (in.)'), ('width', 'Package Width (in.)'),
                 ('height', 'Package Height (in.)'), ('packageWeight', 'Package Weight (lbs.)')]:
        v = dajian_dims.get(k)
        if v and s not in specs:
            specs[s] = str(v)
            changed = True
    
    # Assembled dims → attributes
    for k, a in [('assembledLength', 'Assembled Length (in.)'), 
                 ('assembledWidth', 'Assembled Width (in.)'),
                 ('assembledHeight', 'Assembled Height (in.)')]:
        v = dajian_dims.get(k)
        if v and a not in attrs:
            attrs[a] = str(v)
            changed = True
    
    # Product weight → attributes (never use package weight as a product-weight fallback)
    product_weight = dajian_dims.get('productWeight')
    package_weight_raw = specs.get('Package Weight (lbs.)')
    try:
        package_weight_num = float(package_weight_raw) if package_weight_raw is not None else None
    except (TypeError, ValueError):
        package_weight_num = None

    if desc_weight and (
        not attrs.get('Product Weight (lbs.)')
        or (
            package_weight_num is not None
            and str(attrs.get('Product Weight (lbs.)')).strip() == str(package_weight_raw).strip()
            and abs(float(desc_weight) - package_weight_num) > 0.5
        )
    ):
        attrs['Product Weight (lbs.)'] = str(desc_weight)
        changed = True
        logger.info(f"  [{sku}] Product Weight corrected from description: {desc_weight}")
    elif product_weight and 'Product Weight (lbs.)' not in attrs:
        attrs['Product Weight (lbs.)'] = str(product_weight)
        changed = True

    dajian_description = dajian_dims.get('description') or ''
    if (
        dajian_description
        and missing_description_context
    ):
        product['description'] = dajian_description
        current_description = dajian_description
        changed = True
        logger.info(f"  [{sku}] Description enriched from Dajian API")
    
    if changed:
        product['attributes'] = attrs
        product['specs'] = specs
        # Persist to DB
        _persist_enriched_data(sku, attrs, specs, description=current_description)
    
    return dajian_dims


def _persist_enriched_data(sku: str, attrs: dict, specs: dict, description: str | None = None):
    """Persist enriched attributes/specs back to the database."""
    from src.db.collection_db import SessionLocal
    from src.db.collection_models import CollectedProduct
    db = SessionLocal()
    try:
        p = db.query(CollectedProduct).filter_by(sku=sku).first()
        if p:
            p.attributes = attrs
            p.specs = specs
            if description is not None:
                p.description = description
            flag_modified(p, 'attributes')
            flag_modified(p, 'specs')
            db.commit()
            logger.info(f"  [{sku}] Enriched dims persisted to DB")
    except Exception as e:
        logger.warning(f"  [{sku}] Failed to persist: {e}")
        db.rollback()
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
# Terapeak Market-Aware Pricing
# ═══════════════════════════════════════════════════════════════

def fetch_market_price(title: str) -> float | None:
    """Fetch market average price from eBay Browse API (Terapeak-style).
    
    Returns median market price for similar products, or None if unavailable.
    """
    try:
        from qwen_optimizer import QwenOptimizer
        api_key = os.getenv("QWEN_API_KEY")
        if not api_key:
            return None
        qwen = QwenOptimizer(api_key=api_key)
        intel = qwen.fetch_market_intelligence(title)
        if intel and intel.get('price_stats'):
            stats = intel['price_stats']
            median = stats.get('median') or stats.get('average')
            if median and median > 0:
                logger.info(f"  [MARKET] median=${median:.2f}, "
                            f"avg=${stats.get('average', 0):.2f}, "
                            f"n={intel.get('total_listings', 0)}")
                return float(median)
    except Exception as e:
        logger.warning(f"  [MARKET] Terapeak fetch failed: {e}")
    return None


def calculate_smart_final_price(product: dict, market_price: float | None = None) -> float:
    """Calculate final price using smart pricing with Terapeak data.
    
    Strategy per USAGE.md:
    - FLOOR_PRICE: market competitive, ~10% margin
    - COMPETITIVE: 5% below market, 10-25% margin  
    - CEILING_PRICE: high market price, ~26% margin
    - STANDARD: no market data, 15% margin
    """
    cb = product.get('cost_breakdown', {})
    total_cost = cb.get('total_dajian_cost', 0)
    current_price = product.get('suggested_price', 0) or product.get('price', 0)
    
    if not total_cost:
        # Recalculate cost if missing
        price = product.get('price', 0)
        shipping = product.get('shipping', 0)
        if price > 0:
            cost_data = PricingEngine.calculate_dajian_cost(price, shipping, is_oversize=True)
            total_cost = cost_data['total_dajian_cost']
        else:
            return current_price  # Can't recalculate
    
    if market_price and market_price > 0:
        smart = PricingEngine.calculate_smart_price(total_cost, market_price)
        final = smart['final_price']
        strategy = smart['strategy']
        margin = smart['margin']
        logger.info(f"  [PRICE] Smart: ${final:.2f} (strategy={strategy}, margin={margin:.1%}, "
                    f"market=${market_price:.2f}, cost=${total_cost:.2f})")
        return final
    else:
        # Standard 15% margin
        result = PricingEngine.calculate_selling_price(total_cost, 0.15)
        final = result['selling_price']
        logger.info(f"  [PRICE] Standard: ${final:.2f} (15% margin, cost=${total_cost:.2f})")
        return final


# ═══════════════════════════════════════════════════════════════
# Validation & Enrichment
# ═══════════════════════════════════════════════════════════════

def validate_product(product: dict) -> list:
    """Run pre-publish checks. Returns list of error strings (empty = OK)."""
    errors = []
    sku = product.get('sku', '?')

    opt = product.get('optimization')
    if not opt:
        errors.append("missing optimization data")
        return errors

    raw_title = opt.get('title', product.get('title', ''))
    title, _ = normalize_listing_title_for_ebay(
        raw_title,
        source_title=product.get('title', ''),
    )
    if not title:
        errors.append("missing title")

    price = product.get('suggested_price', 0) or product.get('price', 0)
    cb = product.get('cost_breakdown', {})
    total_cost = cb.get('total_dajian_cost', 0)
    
    # 确保有成本数据或价格可以计算最终价格
    if not price and not total_cost:
        errors.append("no price or cost data to calculate final price")
    elif total_cost and price and price < total_cost:
        # 仅警告，不阻止发布 — calculate_smart_final_price 会重新计算
        logger.warning(f"  [{sku}] suggested_price ${price:.2f} below cost ${total_cost:.2f} — will be recalculated")

    images = product.get('images', [])
    if not images:
        errors.append("no images")

    desc = opt.get('description', product.get('description', ''))
    if not desc:
        errors.append("missing description")

    category_id = str(opt.get('categoryId', '') or '')
    if not category_id:
        errors.append("missing categoryId")

    aspects = opt.get('aspects', {}) or {}
    missing_measurements = [
        key for key in REQUIRED_MEASUREMENT_ASPECT_KEYS
        if not aspects.get(key)
    ]
    if missing_measurements:
        logger.warning(
            f"  [{sku}] Stored optimization is missing measurements: {', '.join(missing_measurements)} "
            "— publish flow will backfill from attributes/description or fall back conservatively."
        )
    placeholder_measurements = [
        issue for issue in _measurement_validation_errors(aspects)
        if issue.startswith('placeholder measurement aspect') or issue.startswith('non-positive measurement aspect')
    ]
    if placeholder_measurements:
        logger.warning(f"  [{sku}] Stored optimization has weak measurement data: {'; '.join(placeholder_measurements)}")

    return errors


def enrich_aspects(product: dict) -> dict:
    """
    Enrich product aspects with dimensions, weight, and auto-detect missing values.
    Returns the completed aspects dict.
    """
    opt = product.get('optimization', {})
    attrs = product.get('attributes', {})
    aspects = dict(opt.get('aspects', {}))
    raw_title = opt.get('title', product.get('title', ''))
    title, _ = normalize_listing_title_for_ebay(
        raw_title,
        source_title=product.get('title', ''),
    )
    title_lower = title.lower()
    cat_id = opt.get('categoryId', '')

    # 1. Brand — generic goods stamp the house brand; art-toy instances keep
    #    each item's own IP brand and default a missing one to "Unbranded".
    if 'Brand' not in aspects or not aspects.get('Brand'):
        aspects['Brand'] = [get_store_profile().default_brand]

    aspects = complete_publish_aspects(
        aspects,
        title=title,
        category_id=cat_id,
        attrs=attrs,
        description=product.get('description', ''),
        category_required_aspects=EbayPublisher.CATEGORY_REQUIRED_ASPECTS,
        log=lambda message: logger.info(f"  {message}"),
    )

    # 8. Pre-publish sanitization: enforce single-value for most aspects
    sanitize_placeholder_aspects(
        aspects,
        title=title,
        category_id=cat_id,
        log=lambda message: logger.info(f"  {message}"),
    )
    sanitize_single_value_aspects(aspects, log=lambda message: logger.info(f"  {message}"))

    return aspects


# ═══════════════════════════════════════════════════════════════
# Publishing
# ═══════════════════════════════════════════════════════════════

def publish_single_product(product: dict, dry_run: bool = False) -> dict:
    """
    Publish a single product to eBay with full pipeline:
      1. Validate  2. Auth  3. Category match  4. Enrich aspects
      5. EPS images  6. Create inventory  7. Create offer  8. Publish  9. Video
    """
    from src.clients.real_ebay_client import RealEbayClient
    from src.services.ebay_policy_manager import EbayPolicyManager
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_category_matcher import EbayCategoryMatcher

    sku = product['sku']
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    publish_image_limit = _publish_image_limit(sku)

    # ── Step 0: Enrich missing dimensions from Dajian API ──
    enrich_product_dimensions(product)

    # ── Pre-validation ──
    errors = validate_product(product)
    if errors:
        msg = f"Validation failed: {'; '.join(errors)}"
        record_error(sku, msg)
        return {"status": "error", "message": msg}

    # ── Auth ──
    oauth = EbayOAuthService(environment)
    if not oauth.is_authorized():
        return {"status": "error", "message": "eBay not authorized"}

    opt = product.get('optimization', {})
    raw_title = opt.get('title', product.get('title', ''))
    title, title_changed = normalize_listing_title_for_ebay(
        raw_title,
        source_title=product.get('title', ''),
    )
    if title_changed:
        stripped_title, removed_supplier_brand, removed_prefix = strip_supplier_brand_prefix(raw_title)
        if removed_supplier_brand:
            logger.info(f"  [TITLE] Removed supplier prefix '{removed_prefix}' for {sku}")
        elif raw_title != title:
            logger.info(f"  [TITLE] Removed non-product title marker(s) for {sku}")
        opt['title'] = title
    description = opt.get('description', product.get('description', ''))
    description = smart_truncate_html(description, max_length=50000, min_length=45000)
    package_weight_for_description = (
        build_package_weight_and_size(
            product.get('attributes', {}) or {},
            product.get('specs', {}) or {},
        ) or {}
    ).get('weight', {}).get('value')
    description = replace_description_weight_placeholder_with_package_weight(
        description,
        package_weight_for_description,
    )

    # ── Step 0b: Smart pricing with Terapeak market data ──
    market_p = fetch_market_price(title)
    final_price = calculate_smart_final_price(product, market_p)

    # Guard: refuse to publish with empty or placeholder description
    if not description or len(description) < 100 or 'Brand new product' in description[:100]:
        logger.warning(f"  {sku}: Description too short or placeholder ({len(description)} chars). Skipping publish.")
        record_error(sku, f"Empty/placeholder description ({len(description)} chars)")
        return {"status": "error", "message": f"Description is empty or placeholder ({len(description)} chars)"}

    # ── Category matching ──
    category_matcher = EbayCategoryMatcher(oauth)
    existing_aspects = dict(opt.get('aspects', {'Brand': [get_store_profile().default_brand]}))
    source_title = (product.get('title') or '').strip()
    category_lookup_title = title or source_title
    title_context = title or source_title
    stored_cat = str(opt.get('categoryId') or '').strip() or None
    stored_cat_name = opt.get('categoryName')
    if stored_cat in INVALID_CATEGORY_REMAP:
        remapped = INVALID_CATEGORY_REMAP[stored_cat]
        logger.warning(f"  [CAT] Remapping legacy invalid stored category {stored_cat} -> {remapped}")
        stored_cat = remapped
    if stored_cat:
        canonical_cat, canonical_name = category_matcher.canonicalize_category(
            title_context,
            stored_cat,
            stored_cat_name,
            product.get('description', ''),
        )
        if canonical_cat != stored_cat:
            logger.warning(f"  [CAT] Canonicalizing stored category {stored_cat} -> {canonical_cat}")
        stored_cat, stored_cat_name = canonical_cat, canonical_name
    if stored_cat and not _is_sellable_leaf_category(oauth, stored_cat):
        logger.warning(f"  [CAT] Stored category {stored_cat} is not a sellable leaf in EBAY_US; ignoring stored category")
        stored_cat = None
        stored_cat_name = None

    # Get auto-matched category + completed aspects
    api_cat_id, api_cat_name, completed_aspects = category_matcher.get_category_and_aspects(
        category_lookup_title, existing_aspects, product.get('description', '')
    )
    if api_cat_id:
        api_cat_id = str(api_cat_id).strip()
        if api_cat_id in INVALID_CATEGORY_REMAP:
            remapped = INVALID_CATEGORY_REMAP[api_cat_id]
            logger.warning(f"  [CAT] Remapping legacy invalid auto category {api_cat_id} -> {remapped}")
            api_cat_id = remapped
        api_cat_id, api_cat_name = category_matcher.canonicalize_category(
            category_lookup_title,
            api_cat_id,
            api_cat_name,
            product.get('description', ''),
        )
        if not _is_sellable_leaf_category(oauth, api_cat_id):
            logger.warning(f"  [CAT] Auto category {api_cat_id} is not a sellable leaf in EBAY_US; ignoring auto category")
            api_cat_id, api_cat_name = None, None

    protected_stored_categories = PROTECTED_STORED_CATEGORY_IDS
    stored_plausible = bool(
        stored_cat and (
            stored_cat in protected_stored_categories
            or category_matcher.is_category_plausible_for_text(title_context, stored_cat, stored_cat_name)
        )
    )
    auto_plausible = bool(
        api_cat_id and category_matcher.is_category_plausible_for_text(
            category_lookup_title,
            api_cat_id,
            api_cat_name,
        )
    )

    if stored_cat and stored_plausible:
        category_id = stored_cat
        category_name = stored_cat_name or api_cat_name or f"Category {stored_cat}"
        if api_cat_id and api_cat_id != stored_cat and auto_plausible:
            logger.info(f"  [CAT] Preserving audited stored category {stored_cat}; ignoring auto-match {api_cat_id}")
        if stored_cat in protected_stored_categories and api_cat_id and api_cat_id != stored_cat:
            completed_aspects = existing_aspects
            logger.warning(f"  [CAT] Preserving stored protected category {stored_cat}; ignoring fallback/API={api_cat_id}")
        elif api_cat_id and api_cat_id != stored_cat:
            required_aspects, recommended_aspects = category_matcher._get_category_aspects(stored_cat)
            completed_aspects = category_matcher._complete_aspects(
                existing_aspects,
                required_aspects,
                recommended_aspects,
                category_lookup_title,
                category_name,
            )
    elif api_cat_id and auto_plausible:
        category_id = api_cat_id
        category_name = api_cat_name
    elif stored_cat:
        msg = f"Stored category {stored_cat} is not plausible for title '{title_context[:120]}'"
        record_error(sku, msg)
        return {"status": "error", "message": msg}
    else:
        msg = "Cannot determine eBay category"
        record_error(sku, msg)
        return {"status": "error", "message": msg}

    # ── Enrich aspects ──
    enriched_product = dict(product)
    enriched_product['optimization'] = dict(opt)
    enriched_product['optimization']['aspects'] = completed_aspects
    enriched_product['optimization']['categoryId'] = category_id
    completed_aspects = enrich_aspects(enriched_product)
    compatibility = analyze_ebay_motors_compatibility(
        category_id=category_id,
        title=title,
        description=description,
        aspects=completed_aspects,
        is_motors_store=get_store_profile().is_motors,
    )
    completed_aspects = apply_compatibility_aspects(category_id, completed_aspects, compatibility)
    sanitize_placeholder_aspects(
        completed_aspects,
        title=title,
        category_id=category_id,
        log=lambda message: logger.info(f"  {message}"),
    )

    quality_opt = _normalize_generated_listing(
        {
            "title": title,
            "description": description,
            "categoryId": category_id,
            "categoryName": category_name,
            "aspects": completed_aspects,
        },
        source_title=source_title or product.get('title', ''),
        source_description=product.get('description', ''),
        attributes=product.get('attributes', {}) or {},
        specs=product.get('specs', {}) or {},
        images=product.get('images', []) or [],
        videos=product.get('videos', []) or [],
        category_matcher=category_matcher,
    )
    title, _ = normalize_listing_title_for_ebay(
        quality_opt.get("title") or title,
        source_title=source_title or product.get('title', ''),
    )
    description = smart_truncate_html(
        quality_opt.get("description") or description,
        max_length=50000,
        min_length=45000,
    )
    category_id = str(quality_opt.get("categoryId") or category_id)
    category_name = quality_opt.get("categoryName") or category_name
    completed_aspects = quality_opt.get("aspects") or completed_aspects
    compatibility = analyze_ebay_motors_compatibility(
        category_id=category_id,
        title=title,
        description=description,
        aspects=completed_aspects,
        is_motors_store=get_store_profile().is_motors,
    )
    completed_aspects = apply_compatibility_aspects(category_id, completed_aspects, compatibility)
    sanitize_placeholder_aspects(
        completed_aspects,
        title=title,
        category_id=category_id,
        log=lambda message: logger.info(f"  {message}"),
    )

    if compatibility.mode != "not_applicable":
        logger.info(f"  [COMPAT] {compatibility.summary}")
        for issue in compatibility.issues:
            logger.warning(f"  [COMPAT] {issue}")

    publish_blockers = []
    publish_blockers.extend(_measurement_validation_errors(completed_aspects))
    publish_blockers.extend(_category_validation_errors(category_matcher, title_context, category_id, category_name))
    qc_candidate = {
        "title": title,
        "description": description,
        "categoryId": category_id,
        "categoryName": category_name,
        "aspects": completed_aspects,
    }
    fs_conn = None
    try:
        fs_conn = sqlite3.connect(str(PROJECT_ROOT / "ebay_collection.db"), timeout=30)
    except Exception as fact_conn_error:
        logger.warning(f"FactSheet connection unavailable for {sku}: {fact_conn_error}")
    try:
        qc_result = run_listing_qc(
            sku=sku,
            candidate=qc_candidate,
            source_title=source_title or product.get('title', ''),
            source_description=product.get('description', ''),
            source_attributes=product.get('attributes', {}) or {},
            source_specs=product.get('specs', {}) or {},
            images=product.get('images', []) or [],
            videos=product.get('videos', []) or [],
            category_matcher=category_matcher,
            fact_sheet_conn=fs_conn,
        )
    finally:
        if fs_conn is not None:
            fs_conn.close()
    publish_blockers.extend(qc_result["blockers"])
    source_fingerprint = qc_result["source_fingerprint"]
    candidate_fingerprint = qc_result["candidate_fingerprint"]

    publish_blockers = list(dict.fromkeys(publish_blockers))
    if publish_blockers:
        msg = "; ".join(publish_blockers)
        record_error(sku, msg)
        return {"status": "error", "message": msg}

    _persist_prepared_listing(
        sku=sku,
        category_id=category_id,
        category_name=category_name,
        title=title,
        description=description,
        aspects=completed_aspects,
        compatibility=compatibility if compatibility.mode != "not_applicable" else None,
    )

    logger.info(f"  Category: {category_id} ({category_name})")
    logger.info(f"  Aspects: {len(completed_aspects)} items")
    logger.info(f"  Price: ${final_price:.2f}")

    # ── Dry run ──
    if dry_run:
        dims = {k: completed_aspects.get(k) for k in
                ['Item Length','Item Width','Item Height','Item Weight']
                if completed_aspects.get(k)}
        return {
            "status": "dry_run",
            "message": f"Would publish at ${final_price:.2f} in cat {category_id} ({category_name})",
            "category_id": category_id,
            "category_name": category_name,
            "aspects_count": len(completed_aspects),
            "dimensions": dims,
            "images": min(len(product.get('images', [])), publish_image_limit),
            "compatibility_mode": compatibility.mode,
            "compatibility_count": len(compatibility.compatible_products),
            "compatibility_issues": compatibility.issues,
            "qc_status": qc_result["status"],
            "qc_ruleset_version": qc_result["ruleset_version"],
            "fact_sheet_version": qc_result["fact_sheet_version"],
            "qc_warnings": qc_result["warnings"],
            "qc_rule_ids": qc_result.get("rule_ids", []),
            "qc_experience_ids": qc_result.get("experience_ids", []),
            "qc_evidence_refs": qc_result.get("evidence_refs", []),
            "candidate_fingerprint": candidate_fingerprint,
            "source_fingerprint": source_fingerprint,
        }

    # Check fingerprint against expected dry-run fingerprint if passed via env or args
    expected_fingerprint = os.getenv(f"EXPECTED_FINGERPRINT_{sku}")
    if expected_fingerprint and candidate_fingerprint != expected_fingerprint:
        msg = f"Candidate fingerprint drifted: expected {expected_fingerprint} vs actual {candidate_fingerprint}"
        record_error(sku, msg)
        return {"status": "error", "message": msg}

    # ── Publish with retries ──
    last_error = None
    eps_images = None  # cache across retries so we don't re-upload
    category_reselected = False
    marketplace_id = _target_marketplace(category_id)
    required_aspect_names = [
        aspect.get("name")
        for aspect in category_matcher._get_category_aspects(category_id)[0]
        if aspect.get("name")
    ]

    for attempt in range(MAX_RETRIES):
        ebay_client = None
        try:
            policy_manager = EbayPolicyManager(oauth)
            ebay_client = RealEbayClient(oauth, policy_manager)

            # Upload images to EPS (once — reuse on retries)
            if eps_images is None:
                raw_images = product.get('images', [])[:publish_image_limit]
                if raw_images:
                    logger.info(f"  Preparing {len(raw_images)} stable image URLs...")
                    eps_images = ebay_client._prepare_inventory_image_urls(
                        sku,
                        raw_images,
                        max_images=publish_image_limit,
                    )
                    logger.info(f"  Images: {len(eps_images)}/{len(raw_images)} ready")
                else:
                    eps_images = []

            if not eps_images:
                return {"status": "error", "message": "No images after EPS upload"}

            video_id = _try_upload_video(product, oauth, sku, title)

            # Create inventory item
            logger.info(f"  Creating inventory item (attempt {attempt+1})...")
            # Build shipping dimensions from product attributes/specs
            attrs = product.get('attributes', {}) or {}
            specs = product.get('specs', {}) or {}
            pws = build_package_weight_and_size(attrs, specs)
            
            inv_product = {
                "title": title,
                "description": description,
                "image_urls": eps_images,
                "price": final_price,
                "quantity": resolve_publish_quantity(sku, logger=logger),
                "condition": "NEW",
                "aspects": completed_aspects,
                "required_aspect_names": required_aspect_names,
            }
            if video_id:
                inv_product["video_urls"] = [video_id]
            if pws:
                inv_product["packageWeightAndSize"] = pws

            # --- Channel split: Motors parts must go via the Trading API -------
            # eBay Motors categories can't be published through the Inventory API
            # (errorId 25005). Trading AddFixedPriceItem on SiteID 100 does it in
            # one call (item + fitment + publish), so branch out entirely here.
            if get_store_profile().listing_channel == "trading":
                trading_product = dict(inv_product)
                trading_product["description"] = description
                trading_product["compatibility"] = (
                    compatibility.compatible_products
                    if compatibility and compatibility.mode != "not_applicable"
                    else []
                )
                logger.info(
                    f"  [TRADING] Motors publish via AddFixedPriceItem "
                    f"(cat {category_id}, {len(trading_product['compatibility'])} fitment)"
                )
                trade = ebay_client.add_fixed_price_item_motors(
                    trading_product,
                    category_id=category_id,
                    price=final_price,
                    quantity=inv_product["quantity"],
                )
                listing_id = trade.get("itemId")
                if not listing_id:
                    return {"status": "error", "message": "Trading publish returned no ItemID"}
                extra_logs = [f"Trading Motors publish (Ack={trade.get('ack')})"]
                if video_id:
                    extra_logs.append(f"Video: {video_id}")
                update_product_status(sku, listing_id, None, category_id, extra_logs)
                logger.info(f"  OK: Published (Trading Motors)! Listing: {listing_id}")
                return {
                    "status": "success",
                    "message": f"Published via Trading! Listing: {listing_id}",
                    "listing_id": listing_id,
                    "channel": "trading",
                }
            # --- default Inventory API path (furniture / general) --------------

            ebay_client.create_or_replace_inventory_item(
                sku=sku,
                product=inv_product,
            )
            _sync_motors_compatibility(ebay_client, sku, compatibility)

            # Create or reuse offer
            offer_id = _create_or_get_offer(
                ebay_client,
                sku,
                final_price,
                category_id,
                listing_description=description,
                marketplace_id=marketplace_id,
            )
            if not offer_id:
                return {"status": "error", "message": "Failed to create/get offer"}

            listing_id, listing_status = _published_listing_for_offer(ebay_client, offer_id)
            if listing_id:
                logger.info(
                    f"  Offer {offer_id} is already published as listing {listing_id}"
                    f" ({listing_status or 'UNKNOWN'}); reusing it."
                )
            else:
                # Publish
                logger.info(f"  Publishing offer {offer_id}...")
                publish_result = ebay_client.publish_offer(offer_id)
                listing_id = publish_result.get("listingId") if publish_result else None

            if not listing_id:
                return {"status": "error", "message": "No listing ID after publish"}

            readback_expectation = build_publish_readback_expectation(
                sku=sku,
                inventory_product=inv_product,
                category_id=category_id,
                offer_id=offer_id,
                listing_id=listing_id,
            )
            try:
                inventory_readback = ebay_client.get_inventory_item(sku)
                offer_readback = ebay_client.get_offer(offer_id)
            except Exception as readback_error:
                readback = {
                    "status": "transport_failure",
                    "passed": False,
                    "issues": [],
                    "transport_failures": [
                        {
                            "code": "readback_exception",
                            "message": str(readback_error),
                        }
                    ],
                    "sku": sku,
                    "offer_id": offer_id,
                    "listing_id": listing_id,
                }
            else:
                readback = verify_publish_readback(
                    readback_expectation,
                    inventory_readback,
                    offer_readback,
                )
            if not readback["passed"]:
                message = (
                    f"Publish readback failed ({readback['status']}): "
                    f"{readback.get('issues') or readback.get('transport_failures')}"
                )
                record_error(sku, message)
                return {
                    "status": "error",
                    "message": message,
                    "write_state": readback["status"],
                    "readback": readback,
                }

            # Update DB
            extra_logs = [f"Video: {video_id}"] if video_id else []
            update_product_status(sku, listing_id, offer_id, category_id, extra_logs)

            return {
                "status": "success",
                "message": f"Published! Listing: {listing_id}",
                "listing_id": listing_id,
                "offer_id": offer_id,
                "category_id": category_id,
                "write_state": "verified",
                "readback": readback,
                "qc_rule_ids": qc_result.get("rule_ids", []),
                "qc_experience_ids": qc_result.get("experience_ids", []),
                "qc_evidence_refs": qc_result.get("evidence_refs", []),
            }

        except Exception as e:
            last_error = str(e)
            logger.warning(f"  [Attempt {attempt+1}/{MAX_RETRIES}] {last_error[:300]}")
            logger.debug(traceback.format_exc())

            if _is_invalid_compatibility_error(last_error) and attempt < MAX_RETRIES - 1:
                logger.info("  [FIX] eBay rejected structured Motors compatibility — retrying without compatibleProducts.")
                if ebay_client is not None:
                    try:
                        ebay_client.delete_product_compatibility(sku)
                    except Exception as clear_exc:
                        logger.warning(f"  [FIX] Failed to clear compatibility payload: {clear_exc}")

                for compat_key in ("Compatible Year", "Compatible Make", "Compatible Model"):
                    if compat_key in completed_aspects:
                        completed_aspects.pop(compat_key, None)

                compatibility.compatible_products = []
                compatibility.mode = "needs_review"
                compatibility.summary = "Structured compatibility disabled after eBay validation failure"
                compatibility.issues = list(compatibility.issues) + [
                    "eBay rejected the generated compatibleProducts payload; retried without structured compatibility."
                ]
                continue

            if _try_fix_missing_product_identifier(last_error, completed_aspects) and attempt < MAX_RETRIES - 1:
                continue

            # Auto-fix: missing aspect or multi-value aspect
            fixed = _try_fix_missing_aspect(last_error, completed_aspects, category_id)
            if fixed and attempt < MAX_RETRIES - 1:
                continue

            # Auto-fix: mixed EPS/non-EPS images — delete existing offer and force re-upload
            if "mixture of Self Hosted and EPS" in last_error and attempt < MAX_RETRIES - 1:
                logger.info("  [FIX] Deleting stale offer to fix mixed image URLs...")
                try:
                    offer_id_to_del = _create_or_get_offer(
                        ebay_client,
                        sku,
                        final_price,
                        category_id,
                        marketplace_id=marketplace_id,
                    )
                    if offer_id_to_del:
                        ebay_client.delete_offer(offer_id_to_del)
                        logger.info(f"  [FIX] Deleted offer {offer_id_to_del}")
                except Exception:
                    pass
                # Force re-upload EPS images (all must be EPS)
                eps_images = None
                continue

            # Auto-fix: invalid category / not a leaf category
            if _is_invalid_category_error(last_error) and attempt < MAX_RETRIES - 1:
                try:
                    deleted = _delete_stale_offers(ebay_client, sku)
                    if deleted:
                        logger.info(f"  [FIX] Deleted {deleted} stale offer(s) after category error")

                    if category_id not in protected_stored_categories and not category_reselected:
                        new_id, new_name, new_aspects = category_matcher.get_category_and_aspects(
                            title, completed_aspects, description
                        )
                        if new_id:
                            new_id = str(new_id).strip()
                            new_id = INVALID_CATEGORY_REMAP.get(new_id, new_id)
                            if not _is_sellable_leaf_category(oauth, new_id):
                                logger.warning(f"  [FIX] Auto reselected category {new_id} is not sellable leaf; keeping current category")
                                new_id = None
                        if new_id and new_id != category_id:
                            logger.info(f"  [FIX] Category {category_id} invalid, switching to API-suggested: {new_id} ({new_name})")
                            category_id, category_name = new_id, new_name
                            completed_aspects.update(new_aspects)
                            marketplace_id = _target_marketplace(category_id)
                            category_reselected = True
                    continue
                except Exception:
                    pass

            if attempt >= MAX_RETRIES - 1:
                break
            time.sleep(2)

    msg = f"Failed after {MAX_RETRIES} attempts: {last_error}"
    record_error(sku, msg)
    return {"status": "error", "message": msg}


def _create_or_get_offer(ebay_client, sku, price, category_id, listing_description=None, marketplace_id=None) -> str:
    """Create a new offer or retrieve existing one."""
    try:
        offer = ebay_client.create_offer(
            sku=sku,
            price=price,
            category_id=category_id,
            listing_description=listing_description,
            marketplace_id=marketplace_id,
        )
        if offer and offer.get("offerId"):
            return offer["offerId"]
    except Exception as e:
        err_str = str(e)
        if "already exists" in err_str:
            m = re.search(r'"offerId","value":"(\d+)"', err_str)
            if m:
                offer_id = m.group(1)
                logger.info(f"  Reusing offer: {offer_id}")
                try:
                    ebay_client.update_offer_category(offer_id, category_id, listing_description=listing_description)
                except Exception:
                    pass
                return offer_id
        raise
    return None


def _published_listing_for_offer(ebay_client, offer_id: str) -> tuple[str | None, str | None]:
    """Return listing info when an offer is already published."""
    offer = ebay_client.get_offer(offer_id) or {}
    if offer.get("status") != "PUBLISHED":
        return None, None

    listing = offer.get("listing") or {}
    listing_id = listing.get("listingId")
    if not listing_id:
        return None, listing.get("listingStatus")
    return listing_id, listing.get("listingStatus")


def _try_upload_video(product, oauth, sku, title) -> str:
    """Best-effort video upload. Returns video_id or None."""
    videos = product.get('videos', [])
    if not videos:
        return None
    try:
        from src.services.ebay_video_uploader import EbayVideoUploader
        uploader = EbayVideoUploader(oauth)
        vid = uploader.upload_video_sync(videos[0], sku, title[:50])
        if vid:
            logger.info(f"  Video: {vid}")
        return vid
    except Exception as e:
        logger.warning(f"  Video failed (non-blocking): {e}")
        return None


def _try_fix_missing_aspect(error_msg: str, aspects: dict, category_id: str) -> bool:
    """Try to auto-fix a missing/invalid aspect error. Returns True if fixed."""
    return try_fix_publish_error(
        error_msg,
        aspects,
        category_id,
        EbayPublisher.CATEGORY_REQUIRED_ASPECTS,
        MEASUREMENT_ASPECT_KEYS,
        log=lambda message: logger.info(f"  {message}"),
    )


def _try_fix_missing_product_identifier(error_msg: str, aspects: dict) -> bool:
    """Add eBay US's official unavailable text after an explicit missing-ID error."""
    for aspect_name in ("UPC", "EAN", "ISBN"):
        if re.search(rf"\b{aspect_name}\s+field\s+is\s+missing\b", error_msg or "", re.IGNORECASE):
            aspects[aspect_name] = [PRODUCT_IDENTIFIER_UNAVAILABLE_TEXT]
            logger.info(
                f"  [FIX] {aspect_name} is required but unavailable; "
                f"using eBay US identifier text '{PRODUCT_IDENTIFIER_UNAVAILABLE_TEXT}'"
            )
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Batch publish READY products to eBay")
    parser.add_argument('--dry-run', action='store_true', help='Preview without publishing')
    parser.add_argument('--sku', type=str, help='Publish single SKU only')
    parser.add_argument('--sku-list', type=str,
                        help='Comma-separated READY SKU allowlist; preserves the given order')
    parser.add_argument('--enrich-only', action='store_true', help='Only enrich dimensions + pricing, no publish')
    parser.add_argument('--limit', type=int, default=0,
                        help='Max products to publish this run (0 = all; safety cap for auto-publish)')
    args = parser.parse_args()

    if args.sku and args.sku_list:
        parser.error('--sku and --sku-list are mutually exclusive')

    logger.info("=" * 60)
    mode = '(DRY RUN)' if args.dry_run else ('(ENRICH ONLY)' if args.enrich_only else '')
    logger.info(f"eBay Batch Publisher {mode}")
    logger.info(f"Log: {LOG_FILE}")
    logger.info("=" * 60)

    sku_filters = None
    if args.sku_list:
        sku_filters = [s.strip() for s in args.sku_list.split(',') if s.strip()]
    products = get_ready_products(args.sku, sku_filters=sku_filters)
    if not products:
        logger.info("No READY products to publish.")
        return

    if args.limit and args.limit > 0 and len(products) > args.limit:
        logger.info(f"Limiting to first {args.limit} of {len(products)} READY products (--limit)")
        products = products[:args.limit]

    logger.info(f"Products: {len(products)}\n")

    # ── Enrich-only mode ──
    if args.enrich_only:
        enriched, no_data = 0, 0
        for i, product in enumerate(products, 1):
            sku = product['sku']
            logger.info(f"[{i}/{len(products)}] {sku}")
            dajian_dims = enrich_product_dimensions(product)
            if dajian_dims:
                enriched += 1
            else:
                no_data += 1
            # Also log smart price
            title, _ = normalize_listing_title_for_ebay(
                product.get('optimization', {}).get('title', '') or product.get('title', ''),
                source_title=product.get('title', ''),
            )
            market_p = fetch_market_price(title)
            smart_price = calculate_smart_final_price(product, market_p)
            current_price = product.get('suggested_price', 0) or product.get('price', 0)
            if abs(smart_price - current_price) > 0.5:
                logger.info(f"  [PRICE] Would change: ${current_price:.2f} → ${smart_price:.2f}")
        logger.info(f"\nEnrich complete: {enriched} enriched, {no_data} no API data, total={len(products)}")
        return

    success, fail = 0, 0
    results = []

    for i, product in enumerate(products, 1):
        sku = product['sku']
        title = (product.get('optimization', {}).get('title', '') or product.get('title', ''))[:60]
        price = product.get('suggested_price', 0) or product.get('price', 0)

        logger.info(f"[{i}/{len(products)}] {sku}")
        logger.info(f"  Title: {title}...")
        logger.info(f"  Price: ${price:.2f}")

        try:
            result = publish_single_product(product, dry_run=args.dry_run)
        except Exception as e:
            logger.error(f"  UNHANDLED: {e}")
            logger.error(traceback.format_exc())
            result = {"status": "error", "message": str(e)[:300]}

        result['sku'] = sku
        results.append(result)

        if result['status'] == 'success':
            success += 1
            logger.info(f"  OK: {result['message']}\n")
        elif result['status'] == 'dry_run':
            logger.info(f"  DRY: {result['message']}\n")
        else:
            fail += 1
            logger.error(f"  FAIL: {result['message']}\n")

        if not args.dry_run and i < len(products):
            time.sleep(PUBLISH_DELAY_SECS)

    # ── Summary ──
    logger.info("=" * 60)
    logger.info("COMPLETE")
    logger.info(f"  Success: {success}  |  Failed: {fail}  |  Total: {len(products)}")
    logger.info("=" * 60)

    results_file = LOG_DIR / f"publish_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    logger.info(f"Results: {results_file}")

    if fail > 0:
        logger.info("\nFailed:")
        for r in results:
            if r['status'] == 'error':
                logger.info(f"  {r['sku']}: {r['message'][:120]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.error(f"FATAL: {e}")
        logging.error(traceback.format_exc())
        sys.exit(1)
