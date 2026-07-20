from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified  # FIX: For JSON field updates
from contextlib import asynccontextmanager, suppress
import uvicorn
import json
import asyncio
import logging
import sys
import re
from datetime import UTC, datetime
from pathlib import Path
from dotenv import load_dotenv

# Note: Removed Windows encoding fix as it can cause issues with uvicorn
# If Unicode issues occur, set PYTHONIOENCODING=utf-8 in environment

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

import os
# Add root to sys.path to allow importing src
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import collection database
from src.db.collection_db import get_db, init_db
from src.db.database_safety import assert_runtime_not_in_maintenance, validate_runtime_database
from src.db.collection_models import CollectedProduct
from src.services.ebay_category_matcher import create_category_matcher
from src.services.taxonomy_constants import PROTECTED_STORED_CATEGORY_IDS
from src.utils.publish_validation import (
    MEASUREMENT_ASPECT_KEYS,
    first_aspect_text as _first_aspect_text,
    get_publish_blockers as _get_publish_blockers,
    measurement_issue as _measurement_issue,
)
from src.utils.publish_autofix import (
    is_invalid_category_error,
    sanitize_single_value_aspects,
    try_fix_publish_error,
)
from src.utils.publish_aspect_completion import complete_publish_aspects
from src.utils.title_sanitizer import strip_supplier_brand_prefix

_QUALITY_GATE_CATEGORY_MATCHER = None


def _get_quality_gate_category_matcher():
    global _QUALITY_GATE_CATEGORY_MATCHER
    if _QUALITY_GATE_CATEGORY_MATCHER is None:
        _QUALITY_GATE_CATEGORY_MATCHER = create_category_matcher(os.getenv("EBAY_ENVIRONMENT", "PRODUCTION"))
    return _QUALITY_GATE_CATEGORY_MATCHER


@asynccontextmanager
async def lifespan(app: FastAPI):
    assert_runtime_not_in_maintenance(Path(__file__).resolve().parent / "logs" / "_maintenance.lock")
    validate_runtime_database(Path(__file__).resolve().parent / "ebay_collection.db")
    video_task = await startup_event()
    try:
        yield
    finally:
        if video_task:
            video_task.cancel()
            with suppress(asyncio.CancelledError):
                await video_task


app = FastAPI(lifespan=lifespan)


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

# Background video linker task
async def periodic_video_linker():
    """Periodically check and link videos that finished processing"""
    import sqlite3
    import json
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_video_uploader import EbayVideoUploader
    
    while True:
        await asyncio.sleep(120)  # Check every 2 minutes
        
        try:
            # Get videos that are UPLOADED but not yet LIVE
            conn = sqlite3.connect('ebay_collection.db')
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT sku, optimization FROM collected_products 
                WHERE optimization LIKE '%"video_id"%' 
                AND optimization LIKE '%"video_status": "UPLOADED"%'
            ''')
            
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                continue
            
            # Check and link videos
            environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
            oauth = EbayOAuthService(environment)
            
            if not oauth.is_authorized():
                continue
            
            uploader = EbayVideoUploader(oauth)
            
            for sku, opt_str in rows:
                try:
                    opt = json.loads(opt_str) if opt_str else {}
                    video_id = opt.get('video_id')
                    
                    if not video_id:
                        continue
                    
                    # Check video status
                    status_data = uploader.get_video_status(video_id)
                    status = status_data.get('status', 'UNKNOWN')
                    
                    if status == 'LIVE':
                        # Update status and link to inventory
                        uploader._update_product_video_status(sku, video_id, 'LIVE')
                        uploader._add_video_to_ebay_inventory(sku, video_id)
                        print(f"[VideoLinker] Linked video for {sku}")
                    elif status in ['BLOCKED', 'FAILED']:
                        uploader._update_product_video_status(sku, video_id, 'FAILED')
                        print(f"[VideoLinker] Video failed for {sku}: {status}")
                        
                except Exception as e:
                    print(f"[VideoLinker] Error checking {sku}: {e}")
                    
        except Exception as e:
            print(f"[VideoLinker] Background task error: {e}")

async def startup_event():
    import logging
    # Force add handler because Uvicorn already configured logging
    root_logger = logging.getLogger()
    handler = logging.FileHandler('debug_server.log', mode='w', encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    
    logging.info("Server starting up (handler forced)...")
    init_db()
    
    # Start background video linker task
    video_task = asyncio.create_task(periodic_video_linker())
    logging.info("Video linker background task started")
    return video_task

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Browser extension needs wildcard
    allow_credentials=False,  # Cannot use credentials with wildcard origin
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import logging
    logging.error(f"[CRITICAL] Global exception: {exc}")
    import traceback
    logging.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": f"Global Error: {str(exc)}"},
    )

# --- Serving Static Dashboard ---
from fastapi.staticfiles import StaticFiles
import os
from pathlib import Path

# Robust Path Resolution
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "src" / "web"

if not WEB_DIR.exists():
    os.makedirs(WEB_DIR, exist_ok=True)
    # Create a dummy index if missing to avoid 404 loop
    with open(WEB_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write("<h1>Dashboard Not Found (Re-create dashboard.html as index.html)</h1>")

app.mount("/dashboard", StaticFiles(directory=str(WEB_DIR), html=True), name="static")

COLLECTION_ISSUE_LOG = BASE_DIR / "logs" / "collection_quality_issues.jsonl"


def _normalize_video_list(videos: List[str]) -> List[str]:
    normalized = []
    seen = set()
    for raw in videos or []:
        url = str(raw or "").strip()
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        dedupe_key = url.split("?", 1)[0]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(url)
    return normalized


def _collection_issue_flags(
    payload: "ProductPayload",
    normalized_images: List[str],
    normalized_videos: List[str],
    source_facts: Dict[str, Any] | None = None,
) -> List[str]:
    from src.utils.listing_quality_gate import build_source_facts, source_preflight_issue_codes

    issues = []
    plain_description = re.sub(r"<[^>]+>", " ", payload.description or "")
    plain_description = re.sub(r"\s+", " ", plain_description).strip()

    if len(normalized_images) <= 1:
        issues.append(f"images={len(normalized_images)}")
    if len(normalized_videos) == 0:
        issues.append("videos=0")
    if len(plain_description) < 80:
        issues.append(f"description_chars={len(plain_description)}")
    if not payload.attributes and not payload.specs:
        issues.append("attributes=0/specs=0")
    if source_facts is None:
        source_facts = build_source_facts(
            source_title=payload.title,
            source_description=payload.description or "",
            attributes=payload.attributes or {},
            specs=payload.specs or {},
            videos=normalized_videos,
        )
    issues.extend(source_preflight_issue_codes(source_facts))

    return list(dict.fromkeys(issues))


def _append_collection_issue_log(
    payload: "ProductPayload",
    issues: List[str],
    normalized_images: List[str],
    normalized_videos: List[str],
    source_facts: Dict[str, Any] | None = None,
    source_category_hint: Dict[str, str] | None = None,
) -> None:
    if not issues:
        return

    COLLECTION_ISSUE_LOG.parent.mkdir(exist_ok=True)
    event = {
        "timestamp": _utcnow_naive().isoformat(),
        "sku": payload.sku,
        "url": payload.url,
        "issues": issues,
        "image_count": len(normalized_images),
        "video_count": len(normalized_videos),
        "attribute_count": len(payload.attributes or {}),
        "spec_count": len(payload.specs or {}),
        "title": payload.title,
    }
    if source_facts:
        event["source_facts"] = {
            "assembly_required": source_facts.get("assembly_required"),
            "assembly_status": source_facts.get("assembly_status"),
            "claims": source_facts.get("claims"),
            "video": source_facts.get("video"),
        }
    if source_category_hint:
        event["source_category_hint"] = source_category_hint
    with COLLECTION_ISSUE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def fetch_dajian_collection_enrichment(sku: str) -> dict:
    """Fetch title/media/description fallbacks from DaJian detail by SKU."""
    try:
        from src.clients.dajian_client import DaJianClient, extract_product_video_urls

        client_id = os.getenv("DAJIAN_API_KEY")
        client_secret = os.getenv("DAJIAN_API_SECRET")
        if not client_id or not client_secret:
            return {}

        dajian = DaJianClient(client_id, client_secret)
        detail = dajian.get_product_detail_by_sku(sku)
        if not detail:
            return {}

        image_candidates = []
        if detail.get("mainImageUrl"):
            image_candidates.append(detail.get("mainImageUrl"))
        image_candidates.extend(detail.get("imageUrls") or [])

        attributes = detail.get("attributes") if isinstance(detail.get("attributes"), dict) else {}
        description = detail.get("description") or ""
        if not description:
            characteristics = [str(item).strip() for item in (detail.get("characteristics") or []) if str(item).strip()]
            blocks = []
            if characteristics:
                blocks.append(
                    "<h3>Key Features</h3><ul>"
                    + "".join(f"<li>{item}</li>" for item in characteristics[:12])
                    + "</ul>"
                )
            if attributes:
                blocks.append(
                    "<h3>Attributes</h3><ul>"
                    + "".join(f"<li><strong>{k}:</strong> {v}</li>" for k, v in attributes.items())
                    + "</ul>"
                )
        from src.utils.report_images import normalize_image_list

        client_id = os.getenv("DAJIAN_API_KEY")
        client_secret = os.getenv("DAJIAN_API_SECRET")
        if not client_id or not client_secret:
            return {}

        dajian = DaJianClient(client_id, client_secret)
        detail = dajian.get_product_detail_by_sku(sku)
        if not detail:
            return {}

        image_candidates = []
        if detail.get("mainImageUrl"):
            image_candidates.append(detail.get("mainImageUrl"))
        image_candidates.extend(detail.get("imageUrls") or [])

        attributes = detail.get("attributes") if isinstance(detail.get("attributes"), dict) else {}
        description = detail.get("description") or ""
        if not description:
            characteristics = [str(item).strip() for item in (detail.get("characteristics") or []) if str(item).strip()]
            blocks = []
            if characteristics:
                blocks.append(
                    "<h3>Key Features</h3><ul>"
                    + "".join(f"<li>{item}</li>" for item in characteristics[:12])
                    + "</ul>"
                )
            if attributes:
                blocks.append(
                    "<h3>Attributes</h3><ul>"
                    + "".join(f"<li><strong>{k}:</strong> {v}</li>" for k, v in attributes.items())
                    + "</ul>"
                )
            description = "".join(blocks)
        title = detail.get("productName") or detail.get("name") or ""
        cleaned_title, _, _ = strip_supplier_brand_prefix(str(title or "").strip())
        videos = extract_product_video_urls(detail)

        return {
            "title": cleaned_title,
            "description": str(description or "").strip(),
            "images": normalize_image_list(image_candidates, max_images=24),
            "videos": _normalize_video_list(videos),
            "attributes": dict(attributes or {}),
        }
    except Exception as e:
        logging.warning(f"[DAJIAN] Failed collection enrichment for {sku}: {e}")
        return {}

# --- Config ---
from src.utils.store_profile import get_store_profile

BRAND_NAME = get_store_profile().brand_name

# --- Models ---
class ProductPayload(BaseModel):
    sku: str
    title: str
    price: float
    shipping: float = 0.0 # NEW: Support Shipping Cost
    stock: int = 0
    description: str = ""
    images: List[str] = []
    videos: List[str] = []
    attributes: Dict[str, Any] = {}
    specs: Dict[str, Any] = {}
    url: str | None = None

# --- Mock eBay Client (As requested) ---
class MockEbayClient:
    def create_or_replace_inventory_item(self, sku: str, product: Dict[str, Any]):
        print(f"[MockEbay] Creating Inventory Item for {sku}...")
        print(f"           Brand: {product.get('aspects', {}).get('Brand')}")
        print(f"           Title: {product.get('title')}")
        return {"sku": sku, "status": "created"}
    
    def create_offer(self, sku: str, price: float):
        print(f"[MockEbay] Creating Offer for {sku} at ${price}...")
        return {"offerId": f"OFFER-{sku}-123", "status": "created"}
    
    def publish_offer(self, offer_id: str):
        print(f"[MockEbay] Publishing Offer {offer_id}...")
        return {"listingId": f"LISTING-{offer_id}-999"}

ebay_client = MockEbayClient()

# Database is now SQLite - no more in-memory dict! 

class PublishRequest(BaseModel):
    sku: str

# --- Background Logic (Analysis Only) ---
def fetch_dajian_dimensions(sku: str) -> dict:
    """从大建 API 获取产品尺寸和重量"""
    try:
        from src.clients.dajian_client import DaJianClient
        from src.utils.dimension_helpers import extract_dajian_measurements
        client_id = os.getenv("DAJIAN_API_KEY")
        client_secret = os.getenv("DAJIAN_API_SECRET")
        if not client_id or not client_secret:
            return {}
        dajian = DaJianClient(client_id, client_secret)
        detail = dajian.get_product_detail_by_sku(sku)
        if not detail:
            return {}
        measurements = extract_dajian_measurements(detail)
        dims = {
            'length': measurements.get('length'),
            'width': measurements.get('width'),
            'height': measurements.get('height'),
            'packageWeight': measurements.get('packageWeight'),
            'assembledLength': measurements.get('assembledLength'),
            'assembledWidth': measurements.get('assembledWidth'),
            'assembledHeight': measurements.get('assembledHeight'),
            'productWeight': measurements.get('productWeight'),
            'weightUnit': measurements.get('weightUnit', 'lb'),
            'lengthUnit': measurements.get('lengthUnit', 'in'),
            'overSizeFlag': measurements.get('overSizeFlag', False),
        }
        logging.info(
            f"[DAJIAN] {sku} pkg=({dims.get('length')},{dims.get('width')},{dims.get('height')},{dims.get('packageWeight')}) "
            f"prod=({dims.get('assembledLength')},{dims.get('assembledWidth')},{dims.get('assembledHeight')},{dims.get('productWeight')})"
        )
        return dims
    except Exception as e:
        logging.warning(f"[DAJIAN] Failed to fetch dims for {sku}: {e}")
        return {}


def scrape_gigacloud_dimensions(url: str) -> dict:
    """从 GigaCloud 产品页面抓取尺寸数据作为后备方案
    Returns dict with keys like 'Assembled Length (in.)', 'Product Weight (lbs.)' etc.
    """
    import re as _re
    import requests as _req
    if not url or 'gigab2b.com' not in url:
        return {}
    try:
        resp = _req.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        text = resp.text
        if 'Safe Checker' in text or 'AliyunCaptcha' in text or 'captcha-container' in text:
            logging.warning(f"[GC-SCRAPE] Anti-bot verification page returned for {url}")
            return {}
        result = {}
        patterns = [
            (r'Assembled Length\s*\(?in\.?\)?[:\s]+([\d.]+)', 'Assembled Length (in.)'),
            (r'Assembled Width\s*\(?in\.?\)?[:\s]+([\d.]+)', 'Assembled Width (in.)'),
            (r'Assembled Height\s*\(?in\.?\)?[:\s]+([\d.]+)', 'Assembled Height (in.)'),
            (r'Product Weight\s*\(?lbs?\.?\)?[:\s]+([\d.]+)', 'Product Weight (lbs.)'),
            # Package dimensions (for specs)
            (r'(?:Package Size|Package)\s*(?:Length|L)\s*\(?in\.?\)?[:\s]+([\d.]+)', 'Package Length (in.)'),
            (r'(?:Package Size|Package)\s*(?:Width|W)\s*\(?in\.?\)?[:\s]+([\d.]+)', 'Package Width (in.)'),
            (r'(?:Package Size|Package)\s*(?:Height|H)\s*\(?in\.?\)?[:\s]+([\d.]+)', 'Package Height (in.)'),
            (r'(?:Package Size|Package)\s*(?:Weight)\s*\(?lbs?\.?\)?[:\s]+([\d.]+)', 'Package Weight (lbs.)'),
            # Alternate format: "Length (in.):38.58"
            (r'(?<![A-Za-z])Length\s*\(?in\.?\)?[:\s]+([\d.]+)', '_pkg_length'),
            (r'(?<![A-Za-z])Width\s*\(?in\.?\)?[:\s]+([\d.]+)', '_pkg_width'),
            (r'(?<![A-Za-z])Height\s*\(?in\.?\)?[:\s]+([\d.]+)', '_pkg_height'),
            (r'(?<![A-Za-z])Weight\s*\(?lbs?\.?\)?[:\s]+([\d.]+)', '_pkg_weight'),
        ]
        for pat, key in patterns:
            m = _re.search(pat, text, _re.IGNORECASE)
            if m and key not in result:
                result[key] = m.group(1)
        if result:
            logging.info(f"[GC-SCRAPE] Scraped dims from page: {result}")
        return result
    except Exception as e:
        logging.warning(f"[GC-SCRAPE] Failed to scrape {url}: {e}")
        return {}


def analyze_product_task(sku: str):
    """Background task to analyze product pricing and run AI optimization"""
    print(f" Analyzing SKU: {sku}...")
    
    # Create new database session for background task
    from src.db.collection_db import SessionLocal
    db = SessionLocal()
    
    try:
        product = db.query(CollectedProduct).filter_by(sku=sku).first()
        if not product:
            print(f"[ERROR] Product {sku} not found in database")
            return

        cleaned_source_title, source_changed, source_removed = strip_supplier_brand_prefix(product.title or "")
        if source_changed:
            product.title = cleaned_source_title
            logging.info(f"[TITLE] {sku}: removed supplier prefix '{source_removed}' from source title")
        
        # 0. 从大建 API 获取产品尺寸（浏览器扩展可能采集不到）
        dajian_dims = fetch_dajian_dimensions(sku)
        if dajian_dims:
            specs = product.specs or {}
            attrs = product.attributes or {}
            # 存储包装尺寸到 specs（用于 eBay shipping）
            for k, s in [('length', 'Package Length (in.)'), ('width', 'Package Width (in.)'),
                         ('height', 'Package Height (in.)'), ('packageWeight', 'Package Weight (lbs.)')]:
                v = dajian_dims.get(k)
                if v:
                    specs[s] = str(v)
            # 存储组装尺寸到 attributes（用于 listing 显示）
            for k, a in [('assembledLength', 'Assembled Length (in.)'), ('assembledWidth', 'Assembled Width (in.)'),
                         ('assembledHeight', 'Assembled Height (in.)')]:
                v = dajian_dims.get(k)
                if v and a not in attrs:
                    try:
                        float(v)  # validate numeric
                        attrs[a] = str(v)
                    except (ValueError, TypeError):
                        pass
            wt = dajian_dims.get('productWeight')
            if wt and 'Product Weight (lbs.)' not in attrs:
                attrs['Product Weight (lbs.)'] = str(wt)
            product.specs = specs
            product.attributes = attrs
            flag_modified(product, 'specs')
            flag_modified(product, 'attributes')
            db.commit()
        
        # 0b. 标准化 attributes 中的重量/尺寸键名
        #     将各种变体统一为标准 key，方便后续 auto-fill
        attrs = product.attributes or {}
        import re as _re
        _attrs_changed = False
        
        # 标准化重量
        if 'Product Weight (lbs.)' not in attrs:
            for alt_key in ['Product Weight', 'Overall Product Weight', 'Net Weight', 'Net  Weight', 'Overall Weight']:
                raw = attrs.get(alt_key)
                if raw:
                    m = _re.search(r'([\d.]+)\s*(?:lbs?|pounds?)?', str(raw), _re.IGNORECASE)
                    if m:
                        attrs['Product Weight (lbs.)'] = m.group(1)
                        _attrs_changed = True
                        print(f"[NORMALIZE] Product Weight (lbs.) = {m.group(1)} (from '{alt_key}')")
                        break
        
        # 标准化尺寸
        for std_key, dim in [('Assembled Length (in.)', 'length'), ('Assembled Width (in.)', 'width'), ('Assembled Height (in.)', 'height')]:
            if std_key not in attrs:
                for alt_key in [f'Overall {dim.capitalize()}', f'Product {dim.capitalize()}']:
                    raw = attrs.get(alt_key)
                    if raw:
                        m = _re.search(r'([\d.]+)\s*(?:in|inch|")?', str(raw), _re.IGNORECASE)
                        if m:
                            attrs[std_key] = m.group(1)
                            _attrs_changed = True
                            print(f"[NORMALIZE] {std_key} = {m.group(1)} (from '{alt_key}')")
                            break
        
        if _attrs_changed:
            product.attributes = attrs
            flag_modified(product, 'attributes')
            db.commit()

        # 0c. 如果 attributes 仍缺少关键组装尺寸/产品重量，尝试从 description 直接解析。
        from src.utils.dimension_helpers import (
            extract_product_dimensions_from_text,
            extract_product_weight_from_text,
        )
        desc_dims = extract_product_dimensions_from_text(product.description or "")
        desc_weight = extract_product_weight_from_text(product.description or "")
        desc_mapping = {
            'Assembled Length (in.)': desc_dims.get('length'),
            'Assembled Width (in.)': desc_dims.get('width'),
            'Assembled Height (in.)': desc_dims.get('height'),
            'Product Weight (lbs.)': desc_weight,
        }
        for attr_key, value in desc_mapping.items():
            if value and attr_key not in attrs:
                attrs[attr_key] = str(value)
                _attrs_changed = True
                print(f"[DESC-FALLBACK] {attr_key} = {value}")

        if _attrs_changed:
            product.attributes = attrs
            flag_modified(product, 'attributes')
            db.commit()

        from src.services.pricing_engine import PricingEngine
        specs = product.specs or {}
        is_oversize = bool(
            dajian_dims.get('overSizeFlag')
            or 'Dimensions' in specs
            or 'Dimensions' in attrs
            or all(specs.get(k) for k in (
                'Package Length (in.)',
                'Package Width (in.)',
                'Package Height (in.)',
            ))
        )
        dajian_costs = PricingEngine.calculate_dajian_cost(
            product_price=product.price,
            shipping_cost=product.shipping,
            is_oversize=is_oversize,
        )
        total_cost = dajian_costs["total_dajian_cost"]
        
        market_price = None  # Default: no market data
        market_avg = None
        market_source = None
        market_sample_size = 0
        market_auth_mode = None
        market_fallback_reason = None
        # Market price lookup via Browse API (best-effort, non-blocking)
        try:
            from scripts.batch_smart_reprice import fetch_market_price, CATEGORY_SEARCH_KEYWORDS
            from src.plugins.terapeak_research.research_client import TerapeakClient
            opt_data_pre = product.optimization or {}
            cat_id_pre = opt_data_pre.get('categoryId', '')
            search_kw = CATEGORY_SEARCH_KEYWORDS.get(str(cat_id_pre))
            if not search_kw:
                # Extract keyword from title (first 3 meaningful words)
                title_words = [w for w in (product.title or '').split()
                              if len(w) > 2 and w.lower() not in ('the', 'and', 'for', 'with', 'new')]
                search_kw = ' '.join(title_words[:3])
            if search_kw:
                market_client = TerapeakClient()
                mkt = fetch_market_price(market_client, search_kw, category_id=str(cat_id_pre) if cat_id_pre else None)
                if mkt.get('median_price') and mkt['median_price'] > 0:
                    market_price = mkt['median_price']
                market_avg = mkt.get('avg_price')
                market_source = mkt.get('source')
                market_sample_size = mkt.get('sample_size', 0)
                market_auth_mode = mkt.get('auth_mode')
                market_fallback_reason = mkt.get('fallback_reason')
                if market_price:
                    print(f"[MARKET] {search_kw} → median=${market_price}, avg=${mkt.get('avg_price', 0):.2f}, n={mkt.get('sample_size', 0)}, src={mkt.get('source')}")
        except Exception as e:
            print(f"[WARN] Market price lookup failed: {e}")
        
        decision = PricingEngine.calculate_smart_price(total_cost, market_price or 0, min_margin=0.10, max_margin=0.35)
        
        # Save pricing breakdown
        dajian_costs['market_price'] = market_price
        dajian_costs['market_avg_price'] = market_avg
        dajian_costs['market_source'] = market_source
        dajian_costs['market_sample_size'] = market_sample_size
        dajian_costs['market_auth_mode'] = market_auth_mode
        dajian_costs['market_fallback_reason'] = market_fallback_reason
        dajian_costs['pricing_strategy'] = decision.get('strategy', 'STANDARD')
        dajian_costs['pricing_margin'] = decision.get('margin')
        dajian_costs['pricing_margin_basis'] = 'net_revenue'
        product.cost_breakdown = dajian_costs
        flag_modified(product, 'cost_breakdown')  # FIX: Notify SQLAlchemy of JSON change
        product.suggested_price = decision["final_price"]
        
        # 2. AI Optimization (Switched to Qwen)
        from qwen_optimizer import QwenOptimizer, optimize_product_full_with_timeout
        import os
        QWEN_KEY = os.getenv("QWEN_API_KEY")
        
        if not QWEN_KEY:
            # Mock optimization
            opt_data = {
                "title": f"MOCK - {product.title}"[:80],
                "description": product.description,
                "aspects": {"Brand": [BRAND_NAME]}
            }
        else:
            qwen = QwenOptimizer(api_key=QWEN_KEY)
            
            # 2a. Fetch market intelligence from eBay (Terapeak-style research)
            market_intel = None
            try:
                market_intel = qwen.fetch_market_intelligence(
                    product_title=product.title,
                    category_id=None  # Auto-detect from search results
                )
                if market_intel:
                    logging.info(f"Market intel fetched: {market_intel.get('total_listings', 0)} listings, {len(market_intel.get('top_keywords', []))} keywords")
            except Exception as mi_err:
                logging.warning(f"Market intelligence fetch failed (non-critical): {mi_err}")
                # Continue without market intelligence — optimization still works fine
            
            opt_data = optimize_product_full_with_timeout(
                api_key=QWEN_KEY,
                original_title=product.title,
                original_description=product.description,
                attributes=attrs,
                specs=product.specs or {},
                images=product.images or [],
                market_intel=market_intel
            )
            cleaned_opt_title, opt_changed, opt_removed = strip_supplier_brand_prefix(opt_data.get("title", ""))
            if opt_changed:
                opt_data["title"] = cleaned_opt_title
                logging.info(f"[TITLE] {sku}: removed supplier prefix '{opt_removed}' from optimized title")

        from src.utils.listing_quality_gate import (
            blocking_issue_messages,
            normalize_generated_listing,
            validate_listing_quality,
        )
        opt_data = normalize_generated_listing(
            opt_data,
            source_title=product.title,
            source_description=product.description or "",
            attributes=attrs,
            specs=product.specs or {},
            images=product.images or [],
            videos=product.videos or [],
            category_matcher=_get_quality_gate_category_matcher(),
        )
        quality_issues = validate_listing_quality(
            opt_data,
            source_title=product.title,
            source_description=product.description or "",
            attributes=attrs,
            specs=product.specs or {},
            images=product.images or [],
            videos=product.videos or [],
            category_matcher=_get_quality_gate_category_matcher(),
        )
        blockers = blocking_issue_messages(quality_issues)
        if blockers:
            raise ValueError("Listing quality gate failed: " + "; ".join(blockers[:8]))
        product.optimization = opt_data
        
        flag_modified(product, 'optimization')  # FIX: Notify SQLAlchemy of JSON change
        
        # Update status
        product.status = "READY"
        product.logs = (product.logs or []) + [f"Analysis complete at {_utcnow_naive().isoformat()}"]
        flag_modified(product, 'logs')  # FIX: Notify SQLAlchemy of JSON change
        
        db.commit()
        logging.info(f"Analysis Complete for {sku}")
        
    except Exception as e:
        logging.error(f"[ERROR] Analysis Failed for {sku}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        if product:
            product.status = "ERROR"
            product.logs = (product.logs or []) + [f"Error: {str(e)}"]
            flag_modified(product, 'logs')  # FIX: Notify SQLAlchemy of JSON change
            db.commit()
    finally:
        db.close()

# --- API Endpoints ---

@app.get("/api/listing-health")
async def listing_health():
    """Quick audit of all published listings — returns category/dimension/aspect issues."""
    import sqlite3 as _sqlite3
    try:
        from scripts.audit_fix_active_listings import audit_single_product, parse_json
        conn = _sqlite3.connect(str(Path(__file__).parent / "ebay_collection.db"))
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT sku, title, attributes, specs, optimization, description "
            "FROM collected_products WHERE status = 'PUBLISHED'"
        ).fetchall()

        results = []
        severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0}
        for row in rows:
            opt = parse_json(row["optimization"])
            desc = opt.get("description", row["description"] or "")
            issues, _ = audit_single_product(
                row["sku"], row["title"], row["attributes"],
                row["specs"], row["optimization"], desc
            )
            if issues:
                max_sev = min(i["severity"] for i in issues)  # CRITICAL < HIGH < MEDIUM alphabetically? No.
                max_sev = "CRITICAL" if any(i["severity"] == "CRITICAL" for i in issues) else \
                          "HIGH" if any(i["severity"] == "HIGH" for i in issues) else "MEDIUM"
                severity_counts[max_sev] = severity_counts.get(max_sev, 0) + 1
                results.append({
                    "sku": row["sku"],
                    "title": (row["title"] or "")[:60],
                    "issues": [{"severity": i["severity"], "detail": i["detail"]} for i in issues]
                })
        conn.close()

        return {
            "status": "success",
            "total_published": len(rows),
            "total_with_issues": len(results),
            "severity_counts": severity_counts,
            "issues": results[:50],  # limit to first 50 for response size
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/products")
async def get_products(db: Session = Depends(get_db)):
    """List all collected products"""
    products = db.query(CollectedProduct).order_by(CollectedProduct.created_at.desc()).all()
    return {"status": "success", "products": [p.to_dict() for p in products]}

@app.post("/api/collect")
async def collect_product(
    payload: ProductPayload,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Receives product from browser extension and saves to database.
    """
    try:
        title = str(payload.title or "").strip()
        description = payload.description or ""
        attributes = dict(payload.attributes or {})
        specs = dict(payload.specs or {})
        from src.utils.report_images import normalize_image_list
        normalized_images = normalize_image_list(payload.images, max_images=24)
        normalized_videos = _normalize_video_list(payload.videos)

        plain_description = re.sub(r"<[^>]+>", " ", description)
        plain_description = re.sub(r"\s+", " ", plain_description).strip()
        needs_enrichment = (
            len(normalized_images) <= 1
            or len(normalized_videos) == 0
            or len(plain_description) < 80
            or (not attributes and not specs)
            or not title
            or title.startswith("Unknown Product")
        )
        if needs_enrichment:
            enrichment = fetch_dajian_collection_enrichment(payload.sku)
            if enrichment.get("title") and (not title or title.startswith("Unknown Product")):
                title = enrichment["title"]
            if enrichment.get("description") and len(plain_description) < 80:
                description = enrichment["description"]
                plain_description = re.sub(r"<[^>]+>", " ", description)
                plain_description = re.sub(r"\s+", " ", plain_description).strip()
            if enrichment.get("images") and len(normalized_images) <= 1:
                normalized_images = enrichment["images"]
            if enrichment.get("videos") and not normalized_videos:
                normalized_videos = enrichment["videos"]
            if enrichment.get("attributes") and not attributes:
                attributes = enrichment["attributes"]

        cleaned_title, removed_supplier_brand, removed_prefix = strip_supplier_brand_prefix(title)
        if removed_supplier_brand:
            logging.info(
                "[COLLECT] %s removed supplier prefix '%s' from title",
                payload.sku,
                removed_prefix,
            )
        title = cleaned_title

        payload.title = title
        payload.description = description
        payload.attributes = attributes
        payload.specs = specs
        payload.images = normalized_images
        payload.videos = normalized_videos

        from src.utils.listing_quality_gate import build_source_facts

        source_facts = build_source_facts(
            source_title=payload.title,
            source_description=payload.description or "",
            attributes=payload.attributes or {},
            specs=payload.specs or {},
            videos=normalized_videos,
        )
        hint_category_id, hint_category_name = _get_quality_gate_category_matcher().get_keyword_category_hint(
            payload.title,
            payload.description or "",
        )
        source_category_hint = None
        if hint_category_id:
            source_category_hint = {
                "categoryId": str(hint_category_id),
                "categoryName": hint_category_name or "",
            }
        issue_flags = _collection_issue_flags(
            payload,
            normalized_images,
            normalized_videos,
            source_facts=source_facts,
        )
        _append_collection_issue_log(
            payload,
            issue_flags,
            normalized_images,
            normalized_videos,
            source_facts=source_facts,
            source_category_hint=source_category_hint,
        )

        print(
            f" Received Product: {payload.sku} "
            f"({len(normalized_images)} imgs, "
            f"{len(normalized_videos)} vids, {len(payload.specs or {})} specs, Shipping: ${payload.shipping})"
        )
        
        # Check if product already exists
        existing = db.query(CollectedProduct).filter_by(sku=payload.sku).first()
        
        if existing:
            # Update existing product
            existing.title = title
            existing.price = payload.price
            existing.shipping = payload.shipping
            existing.stock = payload.stock
            existing.description = description
            existing.images = normalized_images
            existing.videos = normalized_videos
            existing.attributes = attributes
            existing.specs = specs
            existing.url = payload.url
            existing.status = "COLLECTED"
            existing.logs = (existing.logs or []) + [f"Updated from extension at {_utcnow_naive().isoformat()}"]
            if issue_flags:
                existing.logs.append(f"Collection quality flags: {', '.join(issue_flags)}")
            flag_modified(existing, 'images')
            flag_modified(existing, 'videos')
            flag_modified(existing, 'attributes')
            flag_modified(existing, 'specs')
            flag_modified(existing, 'logs')
            db.commit()
            print(f" Updated existing product: {payload.sku}")
        else:
            # Create new product
            new_product = CollectedProduct(
                sku=payload.sku,
                title=title,
                price=payload.price,
                shipping=payload.shipping,
                stock=payload.stock,
                description=description,
                images=normalized_images,
                videos=normalized_videos,
                attributes=attributes,
                specs=specs,
                url=payload.url,
                status="COLLECTED",
                logs=[f"Received from extension at {_utcnow_naive().isoformat()}"]
            )
            if issue_flags:
                new_product.logs.append(f"Collection quality flags: {', '.join(issue_flags)}")
            db.add(new_product)
            db.commit()
            print(f"?Created new product: {payload.sku}")
        
        # Schedule background optimization
        print(f"?Scheduling background optimization for {payload.sku}...")
        background_tasks.add_task(analyze_product_task, payload.sku)
        
        return {
            "status": "success",
            "message": "Product collected successfully",
            "sku": payload.sku
        }
        
    except Exception as e:
        print(f"[ERROR] Error collecting product: {e}")
        import traceback
        traceback.print_exc()
        
        # Return JSON error instead of raising exception
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"Failed to collect product: {str(e)}",
                "error": str(e)
            }
        )


@app.post("/api/delist/{sku}")
async def delist_product(sku: str, db: Session = Depends(get_db)):
    """
    下架eBay listing并清空数据，便于重新采集和发布
    
    流程:
    1. 通过 Inventory API 撤回(withdraw)并删除 offer
    2. 删除 inventory item
    3. 重置数据库状态为 PENDING，清除 optimization/listing_id 等字段
    """
    product = db.query(CollectedProduct).filter_by(sku=sku).first()
    if not product:
        return JSONResponse(status_code=404, content={"error": f"SKU {sku} not found"})
    
    steps = []
    
    # Step 1: Delist from eBay (if published)
    if product.status == 'PUBLISHED' and product.listing_id:
        try:
            from src.clients.real_ebay_client import create_real_ebay_client
            ebay_client = create_real_ebay_client()
            result = ebay_client.delist_sku(sku)
            steps.extend(result.get("steps", []))
            
            if not result["success"]:
                return JSONResponse(
                    status_code=500,
                    content={"error": f"Delist failed: {result.get('error')}", "steps": steps}
                )
        except Exception as e:
            steps.append(f"eBay delist error: {str(e)}")
            # Continue to reset DB even if eBay delist fails
    else:
        steps.append(f"SKU {sku} status={product.status}, no eBay delist needed")
    
    # Step 2: Reset DB fields
    old_status = product.status
    old_listing = product.listing_id
    
    product.status = 'PENDING'
    product.listing_id = None
    product.optimization = None
    product.suggested_price = None
    product.cost_breakdown = None
    product.published_at = None
    product.logs = None
    
    flag_modified(product, 'optimization')
    flag_modified(product, 'cost_breakdown')
    flag_modified(product, 'logs')
    
    db.commit()
    steps.append(f"DB reset: {old_status} → PENDING, listing_id {old_listing} → None")
    
    return {
        "status": "success",
        "message": f"SKU {sku} delisted and reset to PENDING",
        "steps": steps
    }


# ===== PRE-PUBLISH VALIDATION =====
def _validate_listing_before_publish(
    sku: str, title_lower: str, title: str,
    aspects: dict, category_id: str, attrs: dict, description: str
) -> list:
    """
    Validate listing data before publishing to catch common errors.
    Returns a list of warning strings. Auto-fixes what it can.
    """
    import re as _re
    from src.utils.dimension_helpers import extract_all_dimensions
    
    warnings = []
    dims = extract_all_dimensions(attrs)
    
    # 1. Validate dimensions match source attributes
    dim_mapping = {
        'Item Length': dims.get('length'),
        'Item Width': dims.get('width'),
        'Item Height': dims.get('height'),
    }
    for aspect_key, source_val in dim_mapping.items():
        if source_val and aspect_key in aspects:
            aspect_val_str = aspects[aspect_key][0] if aspects[aspect_key] else ""
            m = _re.search(r'(\d+\.?\d*)', aspect_val_str)
            if m:
                aspect_num = float(m.group(1))
                if abs(aspect_num - source_val) > 1.0:
                    warnings.append(
                        f"DIMENSION MISMATCH: {aspect_key} aspect={aspect_num} vs source={source_val} — FIXING"
                    )
                    aspects[aspect_key] = [f"{source_val} in"]
    
    # 2. Validate category matches product type
    CATEGORY_PRODUCT_CONFLICTS = {
        "88057": {  # Home Office Desks
            "blockers": ["safe", "gun safe", "security"],
            "correct_category": "20584",
        },
        "38221": {  # Shoe Storage
            "blockers": ["safe", "gun safe", "security safe", "lockbox"],
            "correct_category": "20584",
        },
        "175758": {  # Bed Frames
            "blockers": ["desk", "table", "chair", "sofa", "safe"],
            "correct_category": None,
        },
    }
    if category_id in CATEGORY_PRODUCT_CONFLICTS:
        conflict = CATEGORY_PRODUCT_CONFLICTS[category_id]
        for blocker in conflict["blockers"]:
            pattern = r'\b' + _re.escape(blocker) + r'\b'
            if _re.search(pattern, title_lower):
                correct = conflict.get("correct_category")
                if correct:
                    warnings.append(
                        f"CATEGORY MISMATCH: '{title[:60]}' in category {category_id} but contains '{blocker}' — should be {correct}"
                    )
                break
    
    # 3. Check for non-applicable aspects
    bed_keywords = ["bed", "bunk", "daybed", "mattress", "headboard", "bed frame"]
    is_bed = any(kw in title_lower for kw in bed_keywords)
    if not is_bed and "Compatible Mattress Size" in aspects:
        warnings.append(f"NON-APPLICABLE ASPECT: 'Compatible Mattress Size' on non-bed product — REMOVING")
        del aspects["Compatible Mattress Size"]
    
    safe_keywords = ["safe", "gun", "security"]
    is_safe = any(kw in title_lower for kw in safe_keywords)
    if not is_safe and "For Gun Type" in aspects:
        warnings.append(f"NON-APPLICABLE ASPECT: 'For Gun Type' on non-safe product — REMOVING")
        del aspects["For Gun Type"]
    
    # 4. Verify description contains correct dimensions
    if dims.get('length') and dims.get('width') and dims.get('height') and description:
        desc_clean = _re.sub(r'<[^>]+>', ' ', description).lower()
        l_str = str(dims['length'])
        w_str = str(dims['width'])
        h_str = str(dims['height'])
        # Check if at least 2 of 3 dimension values appear in description
        found_count = sum(1 for d in [l_str, w_str, h_str] if d in desc_clean)
        if found_count < 2:
            warnings.append(
                f"DESC DIMENSION WARNING: Only {found_count}/3 dimension values found in description "
                f"(expected: {l_str}×{w_str}×{h_str})"
            )
    
    return warnings


@app.get("/api/cro/delist/confirm")
async def cro_delist_confirm(sku: str, token: str, db: Session = Depends(get_db)):
    """S25 — magic-link 人工确认下架. 校验 HMAC token + 调 delist_sku."""
    from scripts.cro_delist import (
        DEFAULT_DB as _DELIST_DB, execute_pending_delist,
    )

    result = execute_pending_delist(_DELIST_DB, sku, token=token)
    if result.get('status') == 'not_pending':
        return JSONResponse(status_code=404,
                            content={"error": f"no pending delist for SKU {sku}"})
    if result.get('status') == 'invalid_or_expired_token':
        return JSONResponse(status_code=403,
                            content={"error": "invalid or expired token"})
    if result.get('status') == 'token_mismatch':
        return JSONResponse(status_code=403,
                            content={"error": "token mismatch"})
    if result.get('status') == 'error':
        return JSONResponse(status_code=500, content={"error": result.get('error', '')})
    return {
        "sku": sku,
        "ok": bool(result.get('ok')),
        "result": result.get('result'),
        "status": result.get('status'),
        "error": result.get('error', ''),
    }


def hmac_safe_eq(a: str, b: str) -> bool:
    import hmac as _h
    return _h.compare_digest(a, b)


@app.post("/api/publish/{sku}")
async def publish_product(sku: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Trigger Final eBay Listing (Confirm & List workflow)
    Uses real eBay API with video upload support
    """
    # Imports moved to try block
    
    product = db.query(CollectedProduct).filter_by(sku=sku).first()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    if product.status == "PUBLISHED":
        return {"status": "error", "message": "Already published", "listing_id": product.listing_id}

    # Internal imports to avoid circular deps if any, and ensure availability
    import os
    from src.clients.real_ebay_client import create_real_ebay_client
    from src.services.ebay_video_uploader import upload_video_background

    # Check if optimization is ready
    if not product.optimization or not product.cost_breakdown:
        return {"status": "error", "message": "AI Analysis not complete yet. Please wait."}

    # Check eBay authorization
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    
    try:
        ebay_client = create_real_ebay_client(environment)
        
        if not ebay_client.oauth.is_authorized():
            raise HTTPException(status_code=401, detail="eBay not authorized. Please authorize at /ebay/auth")
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize eBay client: {e}")

    # Proceed to List — with auto-fix retry (up to 3 attempts)
    import re as _re
    from src.services.ebay_category_matcher import EbayCategoryMatcher
    from src.services.ebay_publisher import EbayPublisher
    from src.utils.dimension_helpers import populate_dimension_aspects, build_package_weight_and_size
    from src.utils.ebay_quantity import resolve_publish_quantity
    from src.utils.html_truncator import smart_truncate_html

    MAX_PUBLISH_RETRIES = 3

    def _auto_fix_error(error_msg: str, aspects: dict, cat_id: str) -> bool:
        """Try to auto-fix a publish error by patching aspects. Returns True if fixed."""
        return try_fix_publish_error(
            error_msg,
            aspects,
            cat_id,
            EbayPublisher.CATEGORY_REQUIRED_ASPECTS,
            MEASUREMENT_ASPECT_KEYS,
            log=print,
        )

    try:
        # FIXED: Ensure we have a valid price
        final_price = product.suggested_price
        if not final_price or final_price <= 0:
            final_price = product.price
            print(f"[WARN] No suggested_price, using original price: ${final_price}")
        
        if not final_price or final_price <= 0:
            raise HTTPException(status_code=400, detail="No valid price found. Please set a price.")
        
        print(f"[PRICE] Using final_price: ${final_price} (suggested: ${product.suggested_price}, original: ${product.price})")
        
        opt_data = product.optimization
        listing_title, removed_supplier_brand, removed_prefix = strip_supplier_brand_prefix(
            opt_data.get("title", product.title)
        )
        if removed_supplier_brand:
            print(f"[TITLE] Removed supplier prefix '{removed_prefix}' for {sku}")
            opt_data["title"] = listing_title
        existing_aspects = opt_data.get("aspects", {"Brand": [BRAND_NAME]})
        source_title = (product.title or "").strip()
        category_lookup_title = source_title or listing_title
        title_context = " ".join(part for part in (source_title, listing_title) if part).strip() or listing_title
        protected_stored_categories = PROTECTED_STORED_CATEGORY_IDS
        
        # ===== AUTO CATEGORY & ITEM SPECIFICS MATCHING =====
        category_matcher = EbayCategoryMatcher(ebay_client.oauth)
        stored_cat = str(opt_data.get("categoryId") or "").strip() or None
        stored_cat_name = opt_data.get("categoryName")
        if stored_cat:
            stored_cat, stored_cat_name = category_matcher.canonicalize_category(
                title_context,
                stored_cat,
                stored_cat_name,
                product.description,
            )
        category_id, category_name, completed_aspects = category_matcher.get_category_and_aspects(
            category_lookup_title,
            existing_aspects,
            product.description
        )
        if category_id:
            category_id, category_name = category_matcher.canonicalize_category(
                category_lookup_title,
                category_id,
                category_name,
                product.description,
            )
        
        stored_plausible = bool(
            stored_cat and (
                stored_cat in protected_stored_categories
                or category_matcher.is_category_plausible_for_text(title_context, stored_cat, stored_cat_name)
            )
        )
        auto_plausible = bool(
            category_id and category_matcher.is_category_plausible_for_text(
                category_lookup_title,
                category_id,
                category_name,
            )
        )

        if stored_cat and stored_plausible:
            if category_id and category_id != stored_cat and auto_plausible:
                print(f"[CAT] Using stored={stored_cat}, API suggested={category_id}")
                completed_aspects = category_matcher._complete_aspects(
                    existing_aspects,
                    *category_matcher._get_category_aspects(stored_cat),
                    category_lookup_title,
                    stored_cat_name or f"Category {stored_cat}"
                )
            category_id = stored_cat
            category_name = stored_cat_name or category_name or f"Category {stored_cat}"
        elif not category_id or not auto_plausible:
            category_id = stored_cat
            if not category_id:
                suggestions = ebay_client.get_category_suggestions(listing_title)
                if suggestions:
                    category_id = suggestions[0].get("category", {}).get("categoryId")
                    print(f"[FALLBACK] Using suggested category: {category_id}")
            completed_aspects = existing_aspects
        
        if category_id:
            print(f"[AUTO] Category: {category_id} ({category_name})")
            print(f"[AUTO] Aspects: {len(completed_aspects)} items (was {len(existing_aspects)})")
        
        # Prepare description
        description = opt_data.get("description", product.description)
        description = smart_truncate_html(description, max_length=50000, min_length=45000)
        required_aspect_names = [
            aspect.get("name")
            for aspect in category_matcher._get_category_aspects(category_id)[0]
            if aspect.get("name")
        ]
        
        # Guard: refuse to publish with empty or placeholder description
        if not description or len(description) < 100 or 'Brand new product' in (description or '')[:100]:
            raise HTTPException(status_code=400,
                detail=f"Description is empty or placeholder ({len(description or '')} chars). Run AI optimization first.")
        
        # Auto-fill Item Specifics from product attributes
        attrs = product.attributes or {}
        title_lower = listing_title.lower()
        
        # FORCE dimensions from source attributes (overwrite AI values)
        populate_dimension_aspects(completed_aspects, attrs)

        existing_weight = _first_aspect_text(completed_aspects, 'Item Weight')
        if existing_weight and _measurement_issue(existing_weight) in {'placeholder', 'non-positive'}:
            completed_aspects.pop('Item Weight', None)
            print(f"[SANITIZE] Dropped invalid Item Weight: {existing_weight}")
        
        # ===== PRE-PUBLISH VALIDATION =====
        validation_warnings = _validate_listing_before_publish(
            sku, title_lower, listing_title,
            completed_aspects, category_id, attrs, description
        )
        if validation_warnings:
            for w in validation_warnings:
                print(f"[PRE-PUBLISH WARNING] {sku}: {w}")
        
        # Fallback: fetch dimensions from DaJian API when attrs have none
        dim_keys = ['Item Length', 'Item Width', 'Item Height', 'Item Weight']
        if not any(completed_aspects.get(k) for k in dim_keys):
            try:
                from src.clients.dajian_client import DaJianClient
                dj = DaJianClient(os.getenv('DAJIAN_API_KEY'), os.getenv('DAJIAN_API_SECRET'))
                detail = dj.get_product_detail_by_sku(sku)
                if detail:
                    dj_dims = {
                        'Item Length': detail.get('assembledLength') or detail.get('length'),
                        'Item Width':  detail.get('assembledWidth')  or detail.get('width'),
                        'Item Height': detail.get('assembledHeight') or detail.get('height'),
                        'Item Weight': detail.get('assembledWeight') or detail.get('weight'),
                    }
                    for k, v in dj_dims.items():
                        if v and not completed_aspects.get(k):
                            unit = 'lbs' if 'Weight' in k else 'in'
                            completed_aspects[k] = [f"{v} {unit}"]
                            print(f"[DAJIAN-DIM] {k} = {v} {unit}")
            except Exception as e:
                print(f"[WARN] DaJian dimension fallback failed: {e}")
        
        completed_aspects = complete_publish_aspects(
            completed_aspects,
            title=listing_title,
            category_id=category_id,
            attrs=attrs,
            description=description,
            category_required_aspects=EbayPublisher.CATEGORY_REQUIRED_ASPECTS,
            log=print,
        )

        sanitize_single_value_aspects(completed_aspects, log=print)

        publish_blockers = _get_publish_blockers(
            category_matcher,
            title_context,
            completed_aspects,
            category_id,
            category_name,
        )
        if publish_blockers:
            raise HTTPException(
                status_code=400,
                detail="; ".join(publish_blockers),
            )
        
        # Upload images to eBay EPS (permanent hosting)
        raw_images = product.images or []
        eps_images = None  # cache across retries
        
        # ── Retry loop with auto-fix ──
        last_error = None
        offer_result = None
        listing_id = None
        
        for attempt in range(MAX_PUBLISH_RETRIES):
            try:
                # Upload images (once — reuse on retries)
                if eps_images is None and raw_images:
                    print(f" Uploading {min(len(raw_images), 24)} images to eBay EPS...")
                    eps_images = ebay_client.upload_images_to_eps(raw_images, max_images=24)
                    print(f"[EPS] {len(eps_images)} images hosted on eBay")
                
                if not eps_images:
                    raise Exception("No images after EPS upload")
                
                # Create Inventory Item
                print(f" Creating inventory item for {sku} (attempt {attempt+1})...")
                # Build shipping dimensions from product attributes/specs
                pws = build_package_weight_and_size(
                    product.attributes or {},
                    product.specs if hasattr(product, 'specs') and product.specs else {},
                )
                
                inv_product = {
                    "title": listing_title[:80],
                    "description": description,
                    "image_urls": eps_images,
                    "price": final_price,
                    "quantity": resolve_publish_quantity(product.sku, logger=logger),
                    "condition": "NEW",
                    "aspects": completed_aspects,
                    "required_aspect_names": required_aspect_names,
                }
                if pws:
                    inv_product["packageWeightAndSize"] = pws
                
                ebay_client.create_or_replace_inventory_item(
                    sku=product.sku,
                    product=inv_product,
                )
                
                # Create Offer
                print(f" Creating offer for {sku}...")
                try:
                    offer_result = ebay_client.create_offer(
                        sku=product.sku,
                        price=final_price,
                        category_id=category_id,
                        listing_description=description
                    )
                except Exception as offer_err:
                    err_str = str(offer_err)
                    if "already exists" in err_str:
                        m = _re.search(r'"offerId","value":"(\d+)"', err_str)
                        if m:
                            offer_result = {"offerId": m.group(1)}
                            print(f"[REUSE] Existing offer: {offer_result['offerId']}")
                            # Update the reused offer's description and category
                            try:
                                ebay_client.update_offer_category(
                                    offer_result['offerId'], category_id,
                                    listing_description=description
                                )
                            except Exception:
                                pass
                    if not offer_result:
                        raise
                
                # Publish Offer
                if offer_result and offer_result.get("offerId"):
                    print(f" Publishing offer {offer_result['offerId']}...")
                    listing = ebay_client.publish_offer(offer_result["offerId"])
                    listing_id = listing.get("listingId") if listing else None
                    
                    if listing_id:
                        live_category_id = str(category_id or "")
                        try:
                            offers = ebay_client.get_offers_by_sku(sku)
                            best_offer = None
                            for offer in offers or []:
                                listing_meta = offer.get("listing") or {}
                                if str(listing_meta.get("listingId") or "") == str(listing_id or ""):
                                    best_offer = offer
                                    break
                            if not best_offer and offers:
                                best_offer = offers[0]
                            if best_offer and best_offer.get("categoryId"):
                                live_category_id = str(best_offer.get("categoryId"))
                        except Exception as cat_err:
                            print(f"[WARN] Failed to load live category for {sku}: {cat_err}")

                        opt_data["publishedCategoryId"] = str(category_id or "")
                        if live_category_id:
                            opt_data["liveCategoryId"] = live_category_id
                            opt_data["categoryId"] = live_category_id
                        product.optimization = opt_data
                        flag_modified(product, 'optimization')
                        break  # Success!
                    else:
                        raise Exception("publish_offer returned no listingId")
                else:
                    raise Exception("Failed to create eBay offer.")
                    
            except Exception as e:
                last_error = str(e) if str(e) else "Unknown error"
                print(f"[ATTEMPT {attempt+1}] Error: {last_error[:200]}")
                
                # Try auto-fix
                if attempt < MAX_PUBLISH_RETRIES - 1:
                    fixed = _auto_fix_error(last_error, completed_aspects, category_id or '')
                    
                    # Auto-fix: mixed EPS/non-EPS images
                    if not fixed and "mixture of Self Hosted and EPS" in last_error:
                        print("[AUTO-FIX] Mixed EPS images — forcing re-upload")
                        try:
                            if offer_result and offer_result.get("offerId"):
                                ebay_client.delete_offer(offer_result["offerId"])
                                offer_result = None
                        except Exception:
                            pass
                        eps_images = None  # force re-upload
                        fixed = True
                    
                    # Auto-fix: invalid category
                    if not fixed and is_invalid_category_error(last_error):
                        try:
                            new_id, new_name, new_aspects = category_matcher.get_category_and_aspects(
                                listing_title, completed_aspects, description
                            )
                            if new_id and new_id != category_id:
                                category_id, category_name = new_id, new_name
                                completed_aspects.update(new_aspects)
                                print(f"[AUTO-FIX] New category: {category_id}")
                                fixed = True
                        except Exception:
                            pass
                    
                    if fixed:
                        print(f"[AUTO-FIX] Retrying (attempt {attempt+2})...")
                        import time as _time
                        _time.sleep(2)
                        continue
                
                # Final attempt failed — fall through
                if attempt >= MAX_PUBLISH_RETRIES - 1:
                    break
        
        # ── Record result ──
        if listing_id:
            product.listing_id = listing_id
            product.status = "PUBLISHED"
            product.published_at = _utcnow_naive()
            product.logs = (product.logs or []) + [
                f"Published successfully! Listing ID: {listing_id}",
                f"Offer ID: {offer_result['offerId']}",
                f"Category ID: {category_id}" if category_id else "No category"
            ]
            flag_modified(product, 'logs')
            db.commit()
            
            # Video upload (best-effort, after publishing)
            video_id = None
            if product.videos and len(product.videos) > 0:
                video_url = product.videos[0]
                video_title = listing_title[:50]
                print(f" Uploading video for {sku}...")
                try:
                    from src.services.ebay_video_uploader import EbayVideoUploader
                    video_uploader = EbayVideoUploader(ebay_client.oauth)
                    video_id = video_uploader.upload_video_sync(video_url, sku, video_title)
                    if video_id:
                        print(f" Video uploaded: {video_id}")
                        product.logs.append(f"Video uploaded: {video_id}")
                    else:
                        product.logs.append("Video upload failed")
                except Exception as video_error:
                    print(f" Video upload error: {video_error}")
                    product.logs.append(f"Video upload error: {str(video_error)[:100]}")
                flag_modified(product, 'logs')
                db.commit()
            
            print(f"[OK] Product {sku} published! Listing ID: {listing_id}")
            return {
                "status": "success",
                "message": f"Product published to eBay! Listing ID: {listing_id}",
                "listing_id": listing_id, 
                "offer_id": offer_result["offerId"],
                "video_id": video_id
            }
        else:
            # All retries exhausted
            error_msg = last_error or "Unknown error"
            product.logs = (product.logs or []) + [f"Publish failed after {MAX_PUBLISH_RETRIES} attempts: {error_msg}"]
            flag_modified(product, 'logs')
            db.commit()
            
            if "authorized" in error_msg.lower() or "token" in error_msg.lower():
                return {"status": "error", "message": "eBay authorization expired. Please re-authorize at /ebay/auth"}
            return {"status": "error", "message": f"Failed to publish after {MAX_PUBLISH_RETRIES} attempts: {error_msg}"}

    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e) if str(e) else "Unknown error occurred"
        product.logs = (product.logs or []) + [f"Publish failed: {error_msg}"]
        flag_modified(product, 'logs')
        db.commit()
        logging.error(f"[ERROR] Failed to publish {sku}: {e}")
        import traceback
        logging.error(traceback.format_exc())
        
        if "authorized" in error_msg.lower() or "token" in error_msg.lower():
            return {"status": "error", "message": "eBay authorization expired. Please re-authorize at /ebay/auth"}
        return {"status": "error", "message": f"Failed to publish: {error_msg}"}


@app.post("/api/mi/audit-and-publish/{sku}")
async def mi_audit_and_publish(sku: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """F3 — Market Intelligence 一键闭环：审计 READY 草稿 + 发布到 eBay。

    封装 audit_fix_ready_drafts.audit_and_fix_ready_drafts(sku_filter=sku) +
    publish_product(sku) 两步，让 Streamlit Tab1 的"🚀 一键发布"按钮免去
    复制命令行步骤。规则:
      - 仅对 READY / READY_TO_PUBLISH 状态有效
      - 已发布直接返回 already_published
      - 审计失败但仍可发布的(unresolved 中无该 SKU)继续推进
      - 审计后无法解决的硬错误直接返回 audit_blocked，不调用发布
    """
    product = db.query(CollectedProduct).filter_by(sku=sku).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if product.status == "PUBLISHED":
        return {
            "status": "already_published",
            "message": "Already published",
            "listing_id": product.listing_id,
        }
    if product.status not in ("READY", "READY_TO_PUBLISH"):
        return {
            "status": "error",
            "step": "precheck",
            "message": f"Status {product.status} is not auditable; only READY drafts qualify.",
        }

    # ---- Step 1: audit ----
    try:
        from scripts.audit_fix_ready_drafts import audit_and_fix_ready_drafts
        audit_report = audit_and_fix_ready_drafts(sku_filter=sku)
    except Exception as exc:
        return {
            "status": "error",
            "step": "audit",
            "message": f"Audit raised: {exc}",
        }

    unresolved = audit_report.get("unresolved", []) or []
    blocked = [u for u in unresolved if (u.get("sku") if isinstance(u, dict) else u) == sku]
    if blocked:
        # F10 — 自动屏蔽 30d（TTL），避免反复尝试同一坏品
        try:
            from src.plugins.terapeak_research.blacklist import auto_blacklist_audit_failures
            auto_blacklist_audit_failures([sku], reason="audit_blocked", ttl_days=30)
        except Exception:
            pass
        return {
            "status": "audit_blocked",
            "step": "audit",
            "message": "Audit could not resolve all issues; refusing to publish.",
            "details": blocked,
            "auto_blacklisted": True,
        }

    # Re-fetch product after audit may have rewritten optimization JSON
    db.expire_all()
    product = db.query(CollectedProduct).filter_by(sku=sku).first()
    if not product:
        return {"status": "error", "step": "audit", "message": "Product vanished after audit"}

    # ---- Step 2: publish (delegate to existing endpoint logic) ----
    try:
        result = await publish_product(sku, background_tasks, db)
    except HTTPException as he:
        return {"status": "error", "step": "publish", "message": he.detail, "code": he.status_code}
    except Exception as exc:
        return {"status": "error", "step": "publish", "message": str(exc)}

    audit_changed = len(audit_report.get("changed", []))
    if isinstance(result, dict):
        result.setdefault("audit", {})
        result["audit"]["changed"] = audit_changed
        result["audit"]["unresolved"] = len(unresolved)
        result["step"] = "publish"
    return result


class MIBatchAuditPublishRequest(BaseModel):
    skus: list[str]
    max_count: int = 50


@app.post("/api/mi/batch-audit-and-publish")
async def mi_batch_audit_and_publish(
    payload: MIBatchAuditPublishRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """F8 — Market Intelligence 批量审计+发布。

    单次审计扫描所有 READY 草稿（一次性，效率比逐 SKU 调 audit 高），然后
    对请求列表里既未阻塞又处于 READY/READY_TO_PUBLISH 的 SKU 顺序发布。
    返回每条 SKU 的结果汇总，不抛异常 — 让 UI 可以直接渲染状态表。
    """
    skus = [s.strip() for s in (payload.skus or []) if s and s.strip()]
    # de-dup, preserve order, hard cap to avoid runaway batches
    seen: set[str] = set()
    skus = [s for s in skus if not (s in seen or seen.add(s))][: max(1, payload.max_count)]
    if not skus:
        return {"status": "error", "message": "skus list is empty"}

    results: list[dict] = []
    counters = {"published": 0, "already_published": 0, "audit_blocked": 0,
                "skipped": 0, "error": 0}

    # ---- Step 1: one-shot audit over all READY drafts (no sku_filter) ----
    # Doing it once is much cheaper than calling audit per SKU.
    try:
        from scripts.audit_fix_ready_drafts import audit_and_fix_ready_drafts
        audit_report = audit_and_fix_ready_drafts()
    except Exception as exc:
        return {"status": "error", "step": "audit", "message": f"Audit raised: {exc}"}

    unresolved = audit_report.get("unresolved", []) or []
    blocked_skus: set[str] = set()
    for u in unresolved:
        s = u.get("sku") if isinstance(u, dict) else u
        if s:
            blocked_skus.add(s)

    # F10 — 自动屏蔽审计阻塞 SKU 30 天
    auto_bl_count = 0
    if blocked_skus:
        try:
            from src.plugins.terapeak_research.blacklist import auto_blacklist_audit_failures
            auto_bl_count = auto_blacklist_audit_failures(
                blocked_skus, reason="audit_blocked", ttl_days=30
            )
        except Exception:
            auto_bl_count = 0

    audit_changed_total = len(audit_report.get("changed", []))

    # Refresh ORM state since audit rewrote optimization JSON
    db.expire_all()

    # ---- Step 2: per-SKU publish ----
    for sku in skus:
        product = db.query(CollectedProduct).filter_by(sku=sku).first()
        if not product:
            results.append({"sku": sku, "status": "skipped", "reason": "not_found"})
            counters["skipped"] += 1
            continue
        if product.status == "PUBLISHED":
            results.append({
                "sku": sku, "status": "already_published",
                "listing_id": product.listing_id,
            })
            counters["already_published"] += 1
            continue
        if product.status not in ("READY", "READY_TO_PUBLISH"):
            results.append({
                "sku": sku, "status": "skipped",
                "reason": f"status={product.status}",
            })
            counters["skipped"] += 1
            continue
        if sku in blocked_skus:
            results.append({"sku": sku, "status": "audit_blocked"})
            counters["audit_blocked"] += 1
            continue

        try:
            r = await publish_product(sku, background_tasks, db)
            r_status = (r.get("status") if isinstance(r, dict) else "") or "unknown"
            entry = {
                "sku": sku,
                "status": r_status,
                "listing_id": (r or {}).get("listing_id"),
                "message": (r or {}).get("message"),
            }
            results.append(entry)
            if r_status == "success":
                counters["published"] += 1
            elif r_status == "already_published":
                counters["already_published"] += 1
            else:
                counters["error"] += 1
        except HTTPException as he:
            results.append({"sku": sku, "status": "error",
                            "message": he.detail, "code": he.status_code})
            counters["error"] += 1
        except Exception as exc:
            results.append({"sku": sku, "status": "error", "message": str(exc)})
            counters["error"] += 1

    return {
        "status": "ok",
        "requested": len(skus),
        "audit": {
            "changed": audit_changed_total,
            "unresolved": len(unresolved),
            "blocked_skus_in_request": sum(1 for s in skus if s in blocked_skus),
            "auto_blacklisted": auto_bl_count,
        },
        "counters": counters,
        "results": results,
    }


# ---------------------------------------------------------------------------
# F13 — 批量审计+发布的异步任务接口
# ---------------------------------------------------------------------------

import uuid as _uuid
import threading as _threading
import json as _json_f13
from pathlib import Path as _Path_f13
from datetime import datetime as _datetime_f13

# F16 — 进程内 job 注册表 + 落盘持久化（reports/mi_jobs.json）
_MI_BATCH_JOBS: dict[str, dict] = {}
_MI_BATCH_JOBS_LOCK = _threading.Lock()
_MI_BATCH_JOBS_MAX = 50  # LRU 上限，防内存膨胀
_MI_JOBS_PATH = _Path_f13(__file__).parent / "reports" / "mi_jobs.json"


def _mi_jobs_persist_locked() -> None:
    """已在持锁内调用。失败不抛——持久化不应阻塞业务。"""
    try:
        _MI_JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MI_JOBS_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json_f13.dump(_MI_BATCH_JOBS, f, ensure_ascii=False, default=str)
        tmp.replace(_MI_JOBS_PATH)  # 原子替换
    except Exception:
        pass


def _mi_jobs_load() -> None:
    """启动时调用，恢复上次进程的 job 状态。"""
    if not _MI_JOBS_PATH.exists():
        return
    try:
        with open(_MI_JOBS_PATH, "r", encoding="utf-8") as f:
            data = _json_f13.load(f)
        if isinstance(data, dict):
            with _MI_BATCH_JOBS_LOCK:
                # 把 running/queued 标记为 interrupted（进程已重启）
                for jid, payload in data.items():
                    if isinstance(payload, dict) and payload.get("status") in ("running", "queued"):
                        payload["status"] = "interrupted"
                        payload["error"] = "FastAPI restarted before job completed"
                    _MI_BATCH_JOBS[jid] = payload
    except Exception:
        pass


def _mi_batch_jobs_set(job_id: str, payload: dict) -> None:
    with _MI_BATCH_JOBS_LOCK:
        _MI_BATCH_JOBS[job_id] = payload
        # 简单 LRU：超过上限删除最旧 1/4
        if len(_MI_BATCH_JOBS) > _MI_BATCH_JOBS_MAX:
            for old_id in list(_MI_BATCH_JOBS.keys())[: _MI_BATCH_JOBS_MAX // 4]:
                _MI_BATCH_JOBS.pop(old_id, None)
        _mi_jobs_persist_locked()


def _mi_batch_jobs_get(job_id: str) -> dict | None:
    with _MI_BATCH_JOBS_LOCK:
        return _MI_BATCH_JOBS.get(job_id)


def _mi_batch_jobs_touch(job_id: str) -> None:
    """job 内部状态原地修改后调用一次，刷盘。"""
    with _MI_BATCH_JOBS_LOCK:
        if job_id in _MI_BATCH_JOBS:
            _mi_jobs_persist_locked()


# 启动时恢复
_mi_jobs_load()


@app.post("/api/mi/batch-audit-and-publish-async")
async def mi_batch_audit_and_publish_async(
    payload: MIBatchAuditPublishRequest,
    background_tasks: BackgroundTasks,
):
    """F13 — 异步版本，立刻返回 job_id；UI 通过 GET /api/mi/batch-job/{id} 轮询。

    关键差异：
    - 创建独立 DB 会话（FastAPI Depends(get_db) 不能用于 BackgroundTask）。
    - 在 background_tasks 里复用同步 publish_product 逻辑，逐 SKU 推进；
      每完成一条立即更新进度，UI 可看到实时进展。
    - 异常隔离：单 SKU 抛错不终止整个 job，状态记入 results。
    """
    job_id = _uuid.uuid4().hex[:12]
    skus = list(dict.fromkeys([(s or "").strip() for s in payload.skus if (s or "").strip()]))
    if payload.max_count and len(skus) > payload.max_count:
        skus = skus[: payload.max_count]

    _mi_batch_jobs_set(job_id, {
        "job_id": job_id,
        "status": "queued",
        "created_at": _datetime_f13.now().isoformat(),
        "started_at": None,
        "finished_at": None,
        "requested": len(skus),
        "progress": {"done": 0, "total": len(skus)},
        "audit": None,
        "counters": {"published": 0, "already_published": 0,
                     "audit_blocked": 0, "skipped": 0, "error": 0},
        "results": [],
        "error": None,
    })

    def _run_job():
        """跑在独立线程里，避免阻塞 uvicorn 事件循环。
        publish_product 是 async — 用一次性 event loop 运行。"""
        import asyncio as _asyncio
        from src.db.collection_db import SessionLocal
        job = _mi_batch_jobs_get(job_id)
        if job is None:
            return
        job["status"] = "running"
        job["started_at"] = _datetime_f13.now().isoformat()

        # 一次性 audit（同步调用 — 已在独立线程，不阻塞主循环）
        try:
            from scripts.audit_fix_ready_drafts import audit_and_fix_ready_drafts
            audit_report = audit_and_fix_ready_drafts()
        except Exception as exc:
            job["status"] = "error"
            job["error"] = f"Audit raised: {exc}"
            job["finished_at"] = _datetime_f13.now().isoformat()
            return

        unresolved = audit_report.get("unresolved", []) or []
        blocked_skus_set: set[str] = set()
        for u in unresolved:
            s = u.get("sku") if isinstance(u, dict) else u
            if s:
                blocked_skus_set.add(s)
        # F10 — 自动屏蔽
        auto_bl_count = 0
        if blocked_skus_set:
            try:
                from src.plugins.terapeak_research.blacklist import auto_blacklist_audit_failures
                auto_bl_count = auto_blacklist_audit_failures(
                    blocked_skus_set, reason="audit_blocked", ttl_days=30
                )
            except Exception:
                pass
        job["audit"] = {
            "changed": len(audit_report.get("changed", [])),
            "unresolved": len(unresolved),
            "blocked_skus_in_request": sum(1 for s in skus if s in blocked_skus_set),
            "auto_blacklisted": auto_bl_count,
        }

        for sku in skus:
            db = SessionLocal()
            try:
                product = db.query(CollectedProduct).filter_by(sku=sku).first()
                if not product:
                    job["results"].append({"sku": sku, "status": "not_found"})
                    job["counters"]["error"] += 1
                elif (product.status or "").upper() == "PUBLISHED":
                    job["results"].append({"sku": sku, "status": "already_published"})
                    job["counters"]["already_published"] += 1
                elif product.status not in ("READY", "READY_TO_PUBLISH"):
                    job["results"].append({"sku": sku, "status": "skipped",
                                           "reason": f"status={product.status}"})
                    job["counters"]["skipped"] += 1
                elif sku in blocked_skus_set:
                    job["results"].append({"sku": sku, "status": "audit_blocked"})
                    job["counters"]["audit_blocked"] += 1
                else:
                    try:
                        r = _asyncio.run(publish_product(sku, background_tasks, db))
                        r_status = (r.get("status") if isinstance(r, dict) else "") or "unknown"
                        entry = {"sku": sku, "status": r_status,
                                 "listing_id": (r or {}).get("listing_id"),
                                 "message": (r or {}).get("message")}
                        job["results"].append(entry)
                        if r_status == "success":
                            job["counters"]["published"] += 1
                        elif r_status == "already_published":
                            job["counters"]["already_published"] += 1
                        else:
                            job["counters"]["error"] += 1
                    except Exception as exc:
                        job["results"].append({"sku": sku, "status": "error",
                                               "message": str(exc)})
                        job["counters"]["error"] += 1
            finally:
                db.close()
            job["progress"]["done"] += 1
            _mi_batch_jobs_touch(job_id)  # F16 — 每条结束刷盘

        job["status"] = "done"
        job["finished_at"] = _datetime_f13.now().isoformat()
        _mi_batch_jobs_touch(job_id)

    # 在独立线程里跑，避免阻塞 event loop（audit 同步耗时几十秒~几分钟）
    _threading.Thread(target=_run_job, name=f"mi-batch-{job_id}", daemon=True).start()
    return {"status": "queued", "job_id": job_id, "requested": len(skus)}


@app.get("/api/mi/batch-job/{job_id}")
async def mi_batch_job_status(job_id: str):
    job = _mi_batch_jobs_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found or expired")
    return job


@app.post("/api/mi/batch-job/{job_id}/retry-errors")
async def mi_batch_job_retry_errors(
    job_id: str,
    background_tasks: BackgroundTasks,
):
    """F20 — 提取已完成 job 中状态为 error 的 SKU，重新排队跑一遍。

    注意：
    - 仅当源 job 状态为 done/error/interrupted 时允许 retry。
    - 不会改源 job 内容；新 job 会引用 parent_job_id 便于追踪。
    """
    src_job = _mi_batch_jobs_get(job_id)
    if src_job is None:
        raise HTTPException(status_code=404, detail="job not found or expired")
    if src_job.get("status") in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"source job is still {src_job.get('status')}, cannot retry",
        )

    error_skus = [
        r.get("sku") for r in (src_job.get("results") or [])
        if isinstance(r, dict) and r.get("status") == "error" and r.get("sku")
    ]
    if not error_skus:
        return {"status": "noop", "message": "no error SKUs to retry",
                "parent_job_id": job_id}

    # 复用主流程，构造请求体（去重、保序）
    retry_payload = MIBatchAuditPublishRequest(skus=error_skus, max_count=len(error_skus))
    new_job_resp = await mi_batch_audit_and_publish_async(retry_payload, background_tasks)
    new_job_id = new_job_resp.get("job_id") if isinstance(new_job_resp, dict) else None

    # 在新 job 上打 parent_job_id 标记（便于 UI 显示链路）
    if new_job_id:
        new_job = _mi_batch_jobs_get(new_job_id)
        if new_job is not None:
            new_job["parent_job_id"] = job_id
            _mi_batch_jobs_touch(new_job_id)

    return {
        "status": "queued",
        "parent_job_id": job_id,
        "retry_count": len(error_skus),
        **(new_job_resp if isinstance(new_job_resp, dict) else {}),
    }


@app.post("/api/sync-listing-status")
async def sync_listing_status_endpoint(force: bool = True):
    """
    同步 eBay 在售链接状态到本地数据库
    调用 eBay GetMyeBaySelling 获取真实在售列表，将已结束的标记为 ENDED
    """
    try:
        from src.services.listing_status_sync import sync_listing_status
        result = sync_listing_status(force_refresh=force)
        return {"status": "success", **result}
    except Exception as e:
        logging.error(f"Listing status sync failed: {e}")
        return {"status": "error", "message": str(e)}


# --- End of Server Logic ---

@app.get("/history")
async def history_page():
    """Serve the history dashboard"""
    return FileResponse(str(WEB_DIR / "history.html"))

# --- eBay OAuth Endpoints ---

@app.get("/ebay/auth")
async def ebay_auth_start():
    """Start eBay OAuth authorization flow"""
    from src.services.ebay_auth import EbayOAuthService
    import os
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    auth_url = oauth.get_authorization_url(state="ebay_auth_state")
    
    # Redirect to eBay authorization page
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=auth_url)

@app.get("/ebay/callback")
async def ebay_auth_callback(
    code: Optional[str] = None,
    ebayktn: Optional[str] = None,  # eBay's actual parameter name
    tknexp: Optional[str] = None,
    username: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None
):
    """Handle eBay OAuth callback"""
    from src.services.ebay_auth import EbayOAuthService
    from fastapi.responses import HTMLResponse
    
    # Log all query parameters for debugging
    print(f" OAuth Callback received:")
    print(f"   code: {code[:20] if code else 'None'}...")
    print(f"   ebayktn: {ebayktn[:20] if ebayktn else 'None'}...")
    print(f"   tknexp: {tknexp}")
    print(f"   username: {username}")
    print(f"   state: {state}")
    print(f"   error: {error}")
    print(f"   error_description: {error_description}")
    
    # eBay uses 'ebayktn' instead of 'code' for some flows
    # Use whichever is present
    auth_code = code or ebayktn
    
    # Check for errors from eBay
    if error:
        error_html = f"""
            <html>
                <head><title>eBay Authorization Failed</title></head>
                <body style="font-family: Arial; text-align: center; padding: 50px;">
                    <h1>[ERROR] eBay Authorization Failed</h1>
                    <p><strong>Error:</strong> {error}</p>
                    <p><strong>Description:</strong> {error_description or 'No description'}</p>
                    <p>Please try again or contact support.</p>
                </body>
            </html>
        """
        return HTMLResponse(content=error_html, status_code=400)
    
    # Check if code is present
    if not auth_code:
        error_html = f"""
            <html>
                <head><title>eBay Authorization Error</title></head>
                <body style="font-family: Arial; text-align: center; padding: 50px;">
                    <h1>[ERROR] Authorization Code Missing</h1>
                    <p>The authorization code was not received from eBay.</p>
                    <p><strong>Received parameters:</strong></p>
                    <ul style="text-align: left; display: inline-block;">
                        <li>code: {code or 'None'}</li>
                        <li>ebayktn: {ebayktn or 'None'}</li>
                        <li>tknexp: {tknexp or 'None'}</li>
                        <li>username: {username or 'None'}</li>
                    </ul>
                    <p>Please try the authorization process again.</p>
                    <a href="/ebay/auth">Try Again</a>
                </body>
            </html>
        """
        return HTMLResponse(content=error_html, status_code=400)
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    try:
        # Exchange code for token
        print(f" Exchanging code for token...")
        print(f"   Using auth_code: {auth_code[:20]}...")
        token_data = oauth.exchange_code_for_token(auth_code)
        print(f"?Token received successfully!")
        
        # Return success page
        return HTMLResponse(content=f"""
            <html>
                <head><title>eBay Authorization Success</title></head>
                <body style="font-family: Arial; text-align: center; padding: 50px;">
                    <h1>?eBay Authorization Successful!</h1>
                    <p>Your eBay account <strong>{username or 'Unknown'}</strong> has been successfully authorized.</p>
                    <p>Token has been saved and will be automatically refreshed.</p>
                    <p>Token expires: {tknexp or 'Unknown'}</p>
                    <p>You can now close this window.</p>
                    <script>
                        setTimeout(() => {{
                            window.close();
                        }}, 3000);
                    </script>
                </body>
            </html>
        """)
    except Exception as e:
        print(f"[ERROR] Token exchange failed: {e}")
        import traceback
        traceback.print_exc()
        
        error_html = f"""
            <html>
                <head><title>eBay Authorization Error</title></head>
                <body style="font-family: Arial; text-align: center; padding: 50px;">
                    <h1>[ERROR] Authorization Failed</h1>
                    <p><strong>Error:</strong> {str(e)}</p>
                    <p><strong>Auth Code Used:</strong> {auth_code[:20] if auth_code else 'None'}...</p>
                    <p>Please check the server logs for details.</p>
                    <a href="/ebay/auth">Try Again</a>
                </body>
            </html>
        """
        return HTMLResponse(content=error_html, status_code=400)

@app.get("/api/ebay/auth/status")
async def ebay_auth_status():
    """Check eBay authorization status"""
    from src.services.ebay_auth import EbayOAuthService
    import os
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    is_authorized = oauth.is_authorized()
    
    return {
        "authorized": is_authorized,
        "environment": environment
    }

@app.get("/api/ebay/policies")
async def get_ebay_policies():
    """Get cached eBay policies"""
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_policy_manager import EbayPolicyManager
    import os
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    if not oauth.is_authorized():
        raise HTTPException(status_code=401, detail="Not authorized. Please authorize eBay first.")
    
    policy_manager = EbayPolicyManager(oauth)
    
    return {
        "policies": policy_manager.get_all_policies(),
        "defaults": {
            "fulfillment": policy_manager.get_default_fulfillment_policy_id(),
            "return": policy_manager.get_default_return_policy_id(),
            "payment": policy_manager.get_default_payment_policy_id()
        }
    }

@app.post("/api/ebay/policies/refresh")
async def refresh_ebay_policies():
    """Fetch and cache eBay policies from API"""
    from src.services.ebay_auth import EbayOAuthService
    from src.services.ebay_policy_manager import EbayPolicyManager
    import os
    
    environment = os.getenv("EBAY_ENVIRONMENT", "SANDBOX")
    oauth = EbayOAuthService(environment)
    
    if not oauth.is_authorized():
        raise HTTPException(status_code=401, detail="Not authorized. Please authorize eBay first.")
    
    policy_manager = EbayPolicyManager(oauth)
    policy_manager.fetch_and_cache_all_policies()
    
    return {"status": "success", "message": "Policies refreshed"}

@app.get("/")
def health_check():
    return {"status": "running", "service": "Ebay Copilot Server"}

if __name__ == "__main__":
    _port = int(os.getenv("SERVER_PORT", str(get_store_profile().server_port)))
    uvicorn.run("server:app", host="0.0.0.0", port=_port, reload=False)
