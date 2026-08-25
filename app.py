"""
Dajian Listing Tool - 统一 Streamlit 应用

功能:
1. eBay OAuth 授权
2. 产品采集 (来自浏览器扩展)
3. AI 优化 (Qwen)
4. 定价计算
5. 发布到 eBay

部署: streamlit run app.py
云部署: Streamlit Cloud (免费 HTTPS)
"""

import streamlit as st
import os
import sys
import json
import sqlite3
import threading
import requests
from datetime import UTC, datetime
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add root to path for imports
root_dir = Path(__file__).parent
sys.path.insert(0, str(root_dir))


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _default_brand_aspects() -> dict:
    from src.utils.store_profile import get_store_profile

    return {"Brand": [get_store_profile().brand_name]}

from src.services.taxonomy_constants import (
    INVALID_CATEGORY_REMAP,
    PROTECTED_STORED_CATEGORY_IDS,
    normalize_legacy_category_id,
)
from src.utils.publish_autofix import (
    is_invalid_category_error,
    sanitize_single_value_aspects,
    try_fix_publish_error,
)
from src.utils.title_sanitizer import normalize_listing_title_for_ebay
from src.utils.publish_aspect_completion import complete_publish_aspects
from src.utils.publish_validation import MEASUREMENT_ASPECT_KEYS
from src.utils.mi_draft_origin import is_mi_draft_product
from src.db.database_safety import (
    DatabaseSafetyError,
    assert_runtime_not_in_maintenance,
    validate_runtime_database,
)

# ============================================================================
# Page Config
# ============================================================================
st.set_page_config(
    page_title="Dajian Listing Tool",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded"
)

try:
    assert_runtime_not_in_maintenance(root_dir / "logs" / "_maintenance.lock")
    _database_safety_report = validate_runtime_database(root_dir / "ebay_collection.db")
except DatabaseSafetyError as exc:
    st.error("数据库安全检查未通过，应用已停止以防止进一步损坏。")
    st.code(str(exc))
    st.stop()

# ============================================================================
# Category Name Lookup
# ============================================================================

CATEGORY_NAMES = {
    # Auto Parts & Accessories
    "262210": "Running Boards & Nerf Bars",
    "174020": "Trailer Hitches",
    "174021": "Hitch Cargo Carriers",
    "262216": "Roof Racks & Cross Bars",
    "262093": "Tailgate Parts",
    "85040": "Bike Trailers",
    # Furniture
    "38208": "Sofas & Couches",
    "175758": "Bed Frames",
    "175754": "Kids/Bunk Beds",
    "175756": "Headboards & Footboards",
    "38204": "Coffee/Dining Tables",
    "107578": "Dining Sets",
    "38199": "Nightstands",
    "38200": "End Tables",
    "38205": "Console Tables",
    "20488": "TV Stands",
    "20487": "Cabinets",
    "3199": "Bookcases",
    "20493": "Display Cabinets",
    "103430": "Armoires & Wardrobes",
    "262017": "Jewelry Organizers",
    "68240": "Wardrobes",
    "183322": "Sideboards & Buffets",
    "114397": "Dressers & Chests of Drawers",
    "20466": "Dressers",
    "88057": "Desks",
    "54235": "Chairs",
    "118218": "Accent Chairs",
    "103431": "Bar Stools & Stools",
    "20877": "Rocking Chairs",
    "177000": "Kitchen Islands",
    "115753": "Kitchen Carts",
    "261263": "Hall Trees & Stands",
    "262980": "Benches",
    "32878": "Bathroom Vanities",
    "42428": "Bathroom Cabinets",
    "20580": "Mirrors",
    "175755": "Clothes Racks",
    "38221": "Shoe Storage",
    "20584": "Safes",
    "175764": "Room Dividers",
    # Outdoor
    "79682": "Patio Chairs",
    "79684": "Outdoor Chairs",
    "79686": "Outdoor Tables",
    "139849": "Patio Furniture Sets",
    "138996": "Outdoor Daybeds",
    "139946": "Fence Panels",
    "85916": "Fire Pits",
    "181000": "Fire Pits (Legacy)",
    "20497": "Planters",
    "20518": "Planters & Pots",
    "29511": "Ornaments & Statues",
    "29514": "Plant Stands",
    "75671": "Wheelbarrows, Carts & Wagons",
    # Pet Supplies
    "121851": "Dog Crates",
    "100411": "Litter Boxes",
    "20740": "Furniture & Scratchers",
    "20744": "Dog Beds",
    "20748": "Fences & Exercise Pens",
    "149074": "Beds, Hammocks & Nesters",
    "116394": "Chicken Coops",
    "63108": "Cages, Hutches & Enclosure",
    "108884": "Dog Houses",
    "116380": "Pet Strollers",
    "116389": "Ramps & Stairs",
    "177788": "Carriers & Totes",
    "116366": "Dog Houses (Legacy)",
    # Sports
    "15280": "Treadmills",
    "57275": "Trampolines",
    "58102": "Exercise Bikes",
    # Luggage
    "16080": "Luggage",
    "16289": "Suitcases",
    # Other
    "131588": "Mattresses",
    "106198": "Air Mattresses",
    "145996": "Inflatable Bouncers",
    "181068": "Pool Covers",
}

_CATEGORY_VALIDITY_CACHE = {}


def remap_legacy_category_id(category_id: str) -> str:
    return normalize_legacy_category_id(category_id)


def _safe_listing_title(title: str, *, source_title: str = "") -> str:
    cleaned, _ = normalize_listing_title_for_ebay(
        title or "",
        source_title=source_title or title or "",
    )
    return cleaned


def is_sellable_leaf_category(oauth, category_id: str) -> bool:
    """Validate category ID against EBAY_US taxonomy tree 0 and ensure it's a leaf."""
    cid = remap_legacy_category_id(category_id)
    if not cid:
        return False
    if cid in _CATEGORY_VALIDITY_CACHE:
        return _CATEGORY_VALIDITY_CACHE[cid]

    try:
        token = oauth.get_application_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        resp = requests.get(
            f"{oauth.api_base}/commerce/taxonomy/v1/category_tree/0/get_category_subtree",
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

def get_category_display(product: dict) -> str:
    """Get category display name, preferring the actual live category when available."""
    opt = product.get('optimization', {})
    if isinstance(opt, str):
        try:
            opt = json.loads(opt)
        except:
            return "未分类"
    if not isinstance(opt, dict):
        return "未分类"

    status = str(product.get('status', '') or '')
    actual_cat_id = str(opt.get('liveCategoryId') or opt.get('publishedCategoryId') or '')
    cat_id = str(opt.get('categoryId', '') or '')

    if status == 'PUBLISHED' and actual_cat_id:
        name = opt.get('liveCategoryName') or CATEGORY_NAMES.get(actual_cat_id, f"Category {actual_cat_id}")
        return f"{name} ({actual_cat_id})"

    if not cat_id:
        return "未分类"

    name = opt.get('categoryName') or CATEGORY_NAMES.get(str(cat_id), f"Category {cat_id}")
    if status in {'READY', 'READY_TO_PUBLISH'}:
        return f"{name} ({cat_id}, 草稿类目)"
    return f"{name} ({cat_id})"

# ============================================================================
# Database Functions (SQLite)
# ============================================================================

def get_db_path():
    """Get database path"""
    return str(root_dir / "ebay_collection.db")

def init_db():
    """Initialize database tables"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS collected_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            shipping REAL DEFAULT 0.0,
            stock INTEGER DEFAULT 99,
            url TEXT,
            images TEXT,
            videos TEXT,
            description TEXT,
            attributes TEXT,
            specs TEXT,
            cost_breakdown TEXT,
            suggested_price REAL,
            optimization TEXT,
            status TEXT DEFAULT 'PENDING',
            listing_id TEXT,
            logs TEXT,
            created_at TEXT,
            updated_at TEXT,
            published_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_product(sku: str) -> dict | None:
    """Get product by SKU"""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM collected_products WHERE sku = ?", (sku,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        product = dict(row)
        # Parse JSON fields
        for field in ['images', 'videos', 'attributes', 'specs', 'cost_breakdown', 'optimization', 'logs']:
            if product.get(field):
                try:
                    product[field] = json.loads(product[field])
                except:
                    pass
        return product
    return None

def get_all_products() -> list:
    """Get all products"""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM collected_products ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    
    products = []
    for row in rows:
        product = dict(row)
        for field in ['images', 'videos', 'attributes', 'specs', 'cost_breakdown', 'optimization', 'logs']:
            if product.get(field):
                try:
                    product[field] = json.loads(product[field])
                except:
                    pass
        products.append(product)
    return products


def _calc_transaction_margin(listing_price: float, total_cost: float,
                             discount_pct: float = 5.0,
                             ebay_fee_rate: float = 0.1325,
                             ad_rate: float = 0.05,
                             fixed_fee: float = 0.30) -> float:
    """Margin on actual transaction price, matching the ad-monitor denominator."""
    if listing_price <= 0 or total_cost <= 0:
        return 0.0
    actual_price = listing_price * (1 - discount_pct / 100.0)
    if actual_price <= 0:
        return 0.0
    net_profit = actual_price - actual_price * ebay_fee_rate - actual_price * ad_rate - fixed_fee - total_cost
    return net_profit / actual_price


def load_latest_reprice_report() -> dict | None:
    """Load the newest smart-reprice report for dashboard display."""
    reports_dir = root_dir / "reports"
    candidates = sorted(reports_dir.glob("reprice_report_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None

    latest = candidates[0]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return None

    summary = data.get("summary", {})
    changed_rows = []
    for row in data.get("results", []):
        if row.get("status") != "updated":
            continue
        total_cost = float(row.get("total_cost") or 0)
        new_price = float(row.get("new_price") or 0)
        changed_rows.append({
            "SKU": row.get("sku", ""),
            "Old Price": round(float(row.get("current_price") or 0), 2),
            "New Price": round(new_price, 2),
            "Change %": round(float(row.get("pct_change") or 0), 1),
            "Net Margin %": round(float(row.get("margin") or 0) * 100, 1),
            "Txn Margin %": round(_calc_transaction_margin(new_price, total_cost) * 100, 1),
            "Strategy": row.get("strategy", ""),
            "Cost": round(total_cost, 2),
            "Market Avg": round(float(row.get("market_avg") or 0), 2),
            "Source": row.get("market_source", ""),
        })

    changed_rows.sort(key=lambda r: abs(r["Change %"]), reverse=True)

    change_reports = data.get("change_reports", {}) or {}
    suffix = latest.stem.replace("reprice_report_", "")
    fallback_csv = reports_dir / f"reprice_changes_{suffix}.csv"
    fallback_html = reports_dir / f"reprice_changes_{suffix}.html"
    return {
        "path": latest,
        "timestamp": data.get("timestamp", ""),
        "mode": data.get("mode", ""),
        "market_mode": data.get("market_mode", ""),
        "marketplace_insights_access": data.get("marketplace_insights_access", {}),
        "summary": summary,
        "changed_rows": changed_rows,
        "changes_csv": Path(change_reports["csv"]) if change_reports.get("csv") else (fallback_csv if fallback_csv.exists() else None),
        "changes_html": Path(change_reports["html"]) if change_reports.get("html") else (fallback_html if fallback_html.exists() else None),
    }


def render_latest_reprice_panel():
    """Show latest repricing summary and changed SKUs in the 8501 dashboard."""
    report = load_latest_reprice_report()
    if not report:
        return

    summary = report["summary"]
    changed_rows = report["changed_rows"]
    changed_count = len(changed_rows)
    avg_net_margin = round(sum(r["Net Margin %"] for r in changed_rows) / changed_count, 1) if changed_count else 0.0
    avg_txn_margin = round(sum(r["Txn Margin %"] for r in changed_rows) / changed_count, 1) if changed_count else 0.0
    min_txn_margin = min((r["Txn Margin %"] for r in changed_rows), default=0.0)

    with st.expander("📉 最近智能定价", expanded=False):
        st.caption(
            f"报告时间: {report['timestamp'][:19]} | 模式: {report['mode']} | "
            f"Market Mode: {report['market_mode']} | 报告: {report['path'].name}"
        )

        probe = report.get("marketplace_insights_access") or {}
        if probe.get("available") is False:
            st.warning(
                f"Marketplace Insights 未启用: {probe.get('reason', 'unknown')} "
                f"(HTTP {probe.get('status_code', '?')})。本次实际使用 {summary.get('market_sources', {})}。"
            )

        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("检查总数", summary.get("total", 0))
        c2.metric("已调价", summary.get("price_changes", 0), delta=f"↑{summary.get('price_up', 0)} / ↓{summary.get('price_down', 0)}")
        c3.metric("未变价", summary.get("no_change", 0))
        c4.metric("净收利润率均值", f"{avg_net_margin:.1f}%")
        c5.metric("成交价利润率均值", f"{avg_txn_margin:.1f}%")
        c6.metric("成交价最低利润率", f"{min_txn_margin:.1f}%")

        st.caption("口径说明: 净收利润率 = 净利润 / 扣费后净收；成交价利润率 = 净利润 / 折后成交价。广告监控使用后者，所以 9.1% 净收利润常会对应约 7.4% 成交价利润。")

        dl1, dl2 = st.columns(2)
        csv_path = report.get("changes_csv")
        html_path = report.get("changes_html")
        if csv_path and csv_path.exists():
            with dl1:
                st.download_button(
                    "下载调价明细 CSV",
                    data=csv_path.read_bytes(),
                    file_name=csv_path.name,
                    mime="text/csv",
                    use_container_width=True,
                )
        if html_path and html_path.exists():
            with dl2:
                st.download_button(
                    "下载调价明细 HTML",
                    data=html_path.read_bytes(),
                    file_name=html_path.name,
                    mime="text/html",
                    use_container_width=True,
                )

        if changed_rows:
            st.dataframe(changed_rows, use_container_width=True, hide_index=True)

def save_product(data: dict):
    """Save or update product"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    # Serialize JSON fields
    for field in ['images', 'videos', 'attributes', 'specs', 'cost_breakdown', 'optimization', 'logs']:
        if field in data and data[field] is not None:
            if not isinstance(data[field], str):
                data[field] = json.dumps(data[field], ensure_ascii=False)
    
    now = _utcnow_naive().isoformat()
    
    # Check if exists
    cursor.execute("SELECT id FROM collected_products WHERE sku = ?", (data['sku'],))
    existing = cursor.fetchone()
    
    if existing:
        # Update
        cursor.execute("""
            UPDATE collected_products SET
                title = ?, price = ?, shipping = ?, stock = ?, url = ?,
                images = ?, videos = ?, description = ?, attributes = ?, specs = ?,
                cost_breakdown = ?, suggested_price = ?, optimization = ?,
                status = ?, listing_id = ?, logs = ?, updated_at = ?, published_at = ?
            WHERE sku = ?
        """, (
            data.get('title'), data.get('price'), data.get('shipping', 0),
            data.get('stock', 99), data.get('url'),
            data.get('images'), data.get('videos'), data.get('description'),
            data.get('attributes'), data.get('specs'),
            data.get('cost_breakdown'), data.get('suggested_price'), data.get('optimization'),
            data.get('status', 'PENDING'), data.get('listing_id'), data.get('logs'),
            now, data.get('published_at'), data['sku']
        ))
    else:
        # Insert
        cursor.execute("""
            INSERT INTO collected_products (
                sku, title, price, shipping, stock, url,
                images, videos, description, attributes, specs,
                cost_breakdown, suggested_price, optimization,
                status, listing_id, logs, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data['sku'], data.get('title'), data.get('price'), data.get('shipping', 0),
            data.get('stock', 99), data.get('url'),
            data.get('images'), data.get('videos'), data.get('description'),
            data.get('attributes'), data.get('specs'),
            data.get('cost_breakdown'), data.get('suggested_price'), data.get('optimization'),
            data.get('status', 'PENDING'), data.get('listing_id'), data.get('logs'),
            now, now
        ))
    
    conn.commit()
    conn.close()

def delete_product(sku: str):
    """Delete product"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute("DELETE FROM collected_products WHERE sku = ?", (sku,))
    conn.commit()
    conn.close()

# ============================================================================
# eBay OAuth Service
# ============================================================================

from src.services.ebay_auth import EbayOAuthService

def get_oauth_service():
    """Get eBay OAuth service"""
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    return EbayOAuthService(environment)

def check_authorization():
    """Check if eBay is authorized"""
    try:
        oauth = get_oauth_service()
        return oauth.is_authorized()
    except:
        return False

# ============================================================================
# Pricing Engine
# ============================================================================

from src.services.pricing_engine import PricingEngine

def calculate_pricing(product: dict) -> dict:
    """Calculate pricing using the same market-aware logic as smart repricing."""
    price = product.get('price', 0)
    shipping = product.get('shipping', 0)
    attributes = product.get('attributes', {})
    specs = product.get('specs', {})
    
    # Check if oversize
    is_oversize = any([
        'dimensions' in str(specs).lower(),
        'dimensions' in str(attributes).lower(),
        'oversize' in str(attributes).lower()
    ])
    
    # Calculate costs
    dajian_costs = PricingEngine.calculate_dajian_cost(price, shipping, is_oversize)
    total_cost = dajian_costs["total_dajian_cost"]

    market_price = None
    market_avg = None
    market_source = None
    market_sample_size = 0
    market_auth_mode = None
    market_fallback_reason = None
    try:
        from scripts.batch_smart_reprice import fetch_market_price, CATEGORY_SEARCH_KEYWORDS
        from src.plugins.terapeak_research.research_client import TerapeakClient

        opt = product.get("optimization", {})
        if isinstance(opt, str):
            try:
                opt = json.loads(opt)
            except Exception:
                opt = {}
        cat_id = str((opt or {}).get("categoryId", "") or "")
        search_kw = CATEGORY_SEARCH_KEYWORDS.get(cat_id)
        if not search_kw:
            title_words = [w for w in (product.get('title', '') or '').split()
                           if len(w) > 2 and w.lower() not in ('the', 'and', 'for', 'with', 'new')]
            search_kw = ' '.join(title_words[:3])
        if search_kw:
            mkt = fetch_market_price(TerapeakClient(), search_kw, category_id=cat_id or None)
            if mkt.get('median_price') and mkt['median_price'] > 0:
                market_price = mkt['median_price']
            market_avg = mkt.get('avg_price')
            market_source = mkt.get('source')
            market_sample_size = mkt.get('sample_size', 0)
            market_auth_mode = mkt.get('auth_mode')
            market_fallback_reason = mkt.get('fallback_reason')
    except Exception:
        pass

    smart = PricingEngine.calculate_smart_price(total_cost, market_price or 0, min_margin=0.10, max_margin=0.35)
    dajian_costs["market_price"] = market_price
    dajian_costs["market_avg_price"] = market_avg
    dajian_costs["market_source"] = market_source
    dajian_costs["market_sample_size"] = market_sample_size
    dajian_costs["market_auth_mode"] = market_auth_mode
    dajian_costs["market_fallback_reason"] = market_fallback_reason
    dajian_costs["pricing_strategy"] = smart["strategy"]
    dajian_costs["pricing_margin"] = smart["margin"]
    dajian_costs["pricing_margin_basis"] = "net_revenue"
    
    return {
        "cost_breakdown": dajian_costs,
        "suggested_price": smart["final_price"],
        "safe_price": smart["ceiling_price"],
        "min_price": smart["floor_price"],
        "strategy": smart["strategy"]
    }

# ============================================================================
# AI Optimizer (Qwen)
# ============================================================================

def run_ai_optimization(product: dict) -> dict:
    """Run Qwen AI optimization"""
    from qwen_optimizer import QwenOptimizer
    
    qwen_key = os.getenv("QWEN_API_KEY")
    if not qwen_key:
        return {
            "title": _safe_listing_title(
                product.get('title', ''),
                source_title=product.get('title', ''),
            ),
            "description": product.get('description', ''),
            "aspects": _default_brand_aspects(),
            "error": "QWEN_API_KEY not set"
        }
    
    try:
        qwen = QwenOptimizer(api_key=qwen_key)
        result = qwen.optimize_product_full(
            original_title=product.get('title', ''),
            original_description=product.get('description', ''),
            attributes=product.get('attributes', {}),
            images=product.get('images', []),
            specs=product.get('specs', {})  # 传入规格信息提取尺寸
        )
        return result
    except Exception as e:
        return {
            "title": _safe_listing_title(
                product.get('title', ''),
                source_title=product.get('title', ''),
            ),
            "description": product.get('description', ''),
            "aspects": _default_brand_aspects(),
            "error": str(e)
        }

# ============================================================================
# eBay Publishing
# ============================================================================

def publish_to_ebay(product: dict) -> dict:
    """Publish product to eBay (basic version)"""
    from src.clients.real_ebay_client import RealEbayClient
    from src.services.ebay_policy_manager import EbayPolicyManager
    from src.utils.store_profile import get_store_profile
    from src.services.vehicle_compatibility import (
        EBAY_MOTORS_CATEGORIES,
        analyze_ebay_motors_compatibility,
        apply_compatibility_aspects,
        serialize_compatibility_analysis,
    )
    
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = get_oauth_service()
    
    if not oauth.is_authorized():
        return {"status": "error", "message": "eBay 未授权，请先完成授权"}
    
    try:
        policy_manager = EbayPolicyManager(oauth)
        ebay_client = RealEbayClient(oauth, policy_manager)
        
        opt_data = product.get('optimization', {})
        if not opt_data:
            return {"status": "error", "message": "产品未优化，请先运行 AI 优化"}
        
        final_price = product.get('suggested_price', 0)
        if not final_price:
            return {"status": "error", "message": "价格未计算"}
        
        sku = product['sku']
        title = _safe_listing_title(
            opt_data.get("title", product.get('title', '')),
            source_title=product.get('title', ''),
        )
        description = opt_data.get("description", product.get('description', ''))
        category_id = remap_legacy_category_id(opt_data.get("categoryId", ""))
        if not is_sellable_leaf_category(oauth, category_id):
            from src.services.ebay_category_matcher import EbayCategoryMatcher
            matcher = EbayCategoryMatcher(oauth)
            matched_id, _, _ = matcher.get_category_and_aspects(
                title,
                opt_data.get("aspects", _default_brand_aspects()),
                description,
            )
            category_id = remap_legacy_category_id(matched_id)
        if not is_sellable_leaf_category(oauth, category_id):
            return {"status": "error", "message": f"类目无效: {category_id or '空'}"}
        aspects = apply_compatibility_aspects(
            category_id,
            opt_data.get("aspects", _default_brand_aspects()),
            analyze_ebay_motors_compatibility(
                category_id=category_id,
                title=title,
                description=description,
                aspects=opt_data.get("aspects", _default_brand_aspects()),
                is_motors_store=get_store_profile().is_motors,
            ),
        )
        compatibility = analyze_ebay_motors_compatibility(
            category_id=category_id,
            title=title,
            description=description,
            aspects=aspects,
            is_motors_store=get_store_profile().is_motors,
        )
        opt_data["aspects"] = aspects
        opt_data["motorsCompatibility"] = serialize_compatibility_analysis(compatibility)
        product["optimization"] = opt_data
        
        # 1. Create Inventory Item
        ebay_client.create_or_replace_inventory_item(
            sku=sku,
            product={
                "title": title,
                "description": description,
                "image_urls": product.get('images', [])[:24],
                "price": final_price,
                "quantity": 1,  # 固定为1避免额度不足
                "condition": "NEW",
                "aspects": aspects
            }
        )
        if compatibility.compatible_products:
            ebay_client.create_or_replace_product_compatibility(sku, compatibility.compatible_products)
        elif category_id in EBAY_MOTORS_CATEGORIES:
            ebay_client.delete_product_compatibility(sku)
        
        # 2. Create Offer
        offer = ebay_client.create_offer(
            sku=sku,
            price=final_price,
            category_id=category_id,
            marketplace_id="EBAY_MOTORS" if category_id in EBAY_MOTORS_CATEGORIES else None,
        )
        
        if not offer or not offer.get("offerId"):
            return {"status": "error", "message": "创建 Offer 失败"}
        
        offer_id = offer["offerId"]
        
        # 3. Publish Offer to make it live
        try:
            publish_result = ebay_client.publish_offer(offer_id)
            listing_id = publish_result.get("listingId")
            
            if listing_id:
                return {
                    "status": "success",
                    "message": f"产品已发布! Listing ID: {listing_id}",
                    "listing_id": listing_id,
                    "offer_id": offer_id
                }
            else:
                return {
                    "status": "error",
                    "message": f"发布失败: {publish_result}"
                }
        except Exception as pub_err:
            return {
                "status": "error", 
                "message": f"Offer创建成功但发布失败: {pub_err}"
            }
            
    except Exception as e:
        return {"status": "error", "message": str(e)}


def pre_publish_qc(product: dict) -> list:
    """Pre-publish quality check. Returns list of issues (empty = pass)."""
    from src.services.ebay_publisher import EbayPublisher
    from src.services.vehicle_compatibility import (
        analyze_ebay_motors_compatibility,
        apply_compatibility_aspects,
    )
    from src.utils.store_profile import get_store_profile
    issues = []
    sku = product.get('sku', '?')
    opt = product.get('optimization', {})
    if isinstance(opt, str):
        try:
            opt = json.loads(opt)
        except Exception:
            opt = {}
    if not isinstance(opt, dict):
        opt = {}

    # 1. Title check
    title = opt.get('title', product.get('title', ''))
    if not title:
        issues.append("❌ 缺少标题")
    elif len(title) > 80:
        issues.append(f"⚠️ 标题过长 ({len(title)} chars, 建议≤80)")

    # 2. Category validation
    cat_id = str(opt.get('categoryId', '') or '')
    known_ids = set(EbayPublisher.CATEGORY_REQUIRED_ASPECTS.keys())
    for rule in EbayPublisher.CATEGORY_RULES:
        known_ids.add(rule[0])
    if not cat_id:
        issues.append("❌ 缺少类目 categoryId")
    elif cat_id not in known_ids:
        issues.append(f"⚠️ 类目 {cat_id} 不在已知列表中，发布时可能降级为 API 匹配")

    # 3. Required aspects check
    if cat_id and cat_id in EbayPublisher.CATEGORY_REQUIRED_ASPECTS:
        cat_config = EbayPublisher.CATEGORY_REQUIRED_ASPECTS[cat_id]
        aspects = opt.get('aspects', {})
        for req in cat_config.get('required', []):
            if req not in aspects and req not in cat_config.get('defaults', {}):
                issues.append(f"⚠️ 缺少必填属性 '{req}' 且无默认值")

    # 4. Price check
    price = product.get('suggested_price') or product.get('price', 0)
    if not price or price <= 0:
        issues.append("❌ 价格无效或未设置")
    elif price < 20:
        issues.append(f"⚠️ 售价 ${price:.2f} 可能过低")

    # 5. Images check
    images = product.get('images', [])
    if not images:
        issues.append("❌ 缺少产品图片")
    elif len(images) <= 1:
        issues.append(f"⚠️ 图片过少 ({len(images)} 张)")

    # 6. Description check
    desc = opt.get('description', product.get('description', ''))
    if not desc:
        issues.append("⚠️ 缺少产品描述")

    # 7. Motors compatibility check
    profile = get_store_profile()
    if cat_id in {"174020", "174021", "262210", "262216", "262093"} or profile.is_motors:
        aspects = apply_compatibility_aspects(
            cat_id,
            opt.get('aspects', {}),
            analyze_ebay_motors_compatibility(
                cat_id,
                title,
                desc,
                opt.get('aspects', {}),
                is_motors_store=profile.is_motors,
            ),
        )
        compatibility = analyze_ebay_motors_compatibility(
            cat_id,
            title,
            desc,
            aspects,
            is_motors_store=profile.is_motors,
        )
        if compatibility.mode == "generic_vehicle":
            issues.append("⚠️ Motors Compatibility 只有文本级车型信息，无法生成完整结构化 fitment")
        elif compatibility.mode == "needs_review":
            issues.append("⚠️ Motors Compatibility 未识别到有效适配信息，请人工复核")

    return issues


def publish_with_auto_category(product: dict) -> dict:
    """
    Publish product to eBay with automatic category matching
    使用统一的发布服务，自动匹配类目和补全 Item Specifics
    带智能重试和错误自动修复
    """
    from src.clients.real_ebay_client import RealEbayClient
    from src.services.ebay_policy_manager import EbayPolicyManager
    from src.services.ebay_category_matcher import EbayCategoryMatcher
    from src.services.ebay_publisher import EbayPublisher
    from src.services.vehicle_compatibility import (
        EBAY_MOTORS_CATEGORIES,
        analyze_ebay_motors_compatibility,
        apply_compatibility_aspects,
        serialize_compatibility_analysis,
    )
    from src.utils.store_profile import get_store_profile
    
    oauth = get_oauth_service()
    
    if not oauth.is_authorized():
        return {"status": "error", "message": "eBay 未授权，请先完成授权"}
    
    sku = product['sku']
    max_retries = 2

    def _sync_live_category(opt_data: dict, listing_id: str, fallback_category_id: str, fallback_category_name: str):
        live_category_id = str(fallback_category_id or '')
        live_category_name = fallback_category_name or CATEGORY_NAMES.get(live_category_id, f"Category {live_category_id}") if live_category_id else ""

        try:
            offers = ebay_client.get_offers_by_sku(sku)
            best_offer = None
            for offer in offers or []:
                listing = offer.get("listing") or {}
                if str(listing.get("listingId") or "") == str(listing_id or ""):
                    best_offer = offer
                    break
            if not best_offer and offers:
                best_offer = offers[0]
            if best_offer:
                live_category_id = str(best_offer.get("categoryId") or live_category_id or "")
                if live_category_id:
                    live_category_name = CATEGORY_NAMES.get(live_category_id, f"Category {live_category_id}")
        except Exception as e:
            print(f"[WARN] Failed to sync live category for {sku}: {e}")

        if fallback_category_id:
            opt_data["publishedCategoryId"] = str(fallback_category_id)
        if live_category_id:
            opt_data["liveCategoryId"] = live_category_id
            opt_data["liveCategoryName"] = live_category_name
            # For already-published listings, prefer showing the actual live category next time.
            opt_data["categoryId"] = live_category_id

    for attempt in range(max_retries):
        completed_aspects = {}
        category_id = ""
        try:
            policy_manager = EbayPolicyManager(oauth)
            ebay_client = RealEbayClient(oauth, policy_manager)
            
            # Handle optimization that might be string (JSON) or dict
            opt_data = product.get('optimization', {})
            if isinstance(opt_data, str):
                try:
                    opt_data = json.loads(opt_data)
                except:
                    opt_data = {}
            if not isinstance(opt_data, dict):
                opt_data = {}
            if not opt_data:
                return {"status": "error", "message": "产品未优化，请先运行 AI 优化"}
            
            final_price = product.get('suggested_price', 0)
            if not final_price:
                final_price = product.get('price', 0)
            if not final_price or final_price <= 0:
                return {"status": "error", "message": "价格未设置"}
            
            existing_aspects = opt_data.get("aspects", _default_brand_aspects())
            
            # ===== AUTO CATEGORY & ITEM SPECIFICS MATCHING =====
            category_matcher = EbayCategoryMatcher(oauth)
            category_id, category_name, completed_aspects = category_matcher.get_category_and_aspects(
                opt_data.get("title", product.get('title', '')),
                existing_aspects,
                product.get('description', '')
            )
            category_id = remap_legacy_category_id(category_id)
            stored_cat = remap_legacy_category_id(opt_data.get("categoryId", ""))
            if stored_cat and not is_sellable_leaf_category(oauth, stored_cat):
                print(f"[AUTO] Stored category {stored_cat} is invalid in current taxonomy, ignoring")
                stored_cat = ""
            if category_id and not is_sellable_leaf_category(oauth, category_id):
                print(f"[AUTO] Matched category {category_id} is invalid in current taxonomy, ignoring")
                category_id = ""

            protected_stored_categories = PROTECTED_STORED_CATEGORY_IDS
            if stored_cat in protected_stored_categories and category_id and category_id != stored_cat:
                print(f"[AUTO] Preserving stored protected category {stored_cat}; ignoring fallback/API {category_id}")
                category_id = stored_cat
                category_name = f"Category {stored_cat}"
                completed_aspects = existing_aspects
            
            if category_id:
                print(f"[AUTO] Category: {category_id} ({category_name})")
                print(f"[AUTO] Aspects: {len(completed_aspects)} items")
            else:
                category_id = stored_cat
                completed_aspects = existing_aspects
                if category_id:
                    print(f"[AUTO] Fallback to stored category: {category_id}")
                else:
                    return {"status": "error", "message": "无法确定有效类目，请先运行草稿审计修复"}
            
            completed_aspects = complete_publish_aspects(
                completed_aspects,
                title=opt_data.get("title", product.get('title', '')),
                category_id=category_id,
                attrs=product.get('attributes', {}),
                description=product.get('description', ''),
                category_required_aspects=EbayPublisher.CATEGORY_REQUIRED_ASPECTS,
                log=print,
            )
            from src.utils.listing_quality_gate import enforce_store_brand_aspect
            enforce_store_brand_aspect(completed_aspects)
            
            # 1. Create Inventory Item
            # 使用智能HTML截断器，保持描述结构完整
            from src.utils.html_truncator import smart_truncate_html
            title = _safe_listing_title(
                opt_data.get("title", product.get('title', '')),
                source_title=product.get('title', ''),
            )
            description = opt_data.get("description", product.get('description', ''))
            description = smart_truncate_html(description, max_length=50000, min_length=45000)
            compatibility = analyze_ebay_motors_compatibility(
                category_id=category_id,
                title=title,
                description=description,
                aspects=completed_aspects,
                is_motors_store=get_store_profile().is_motors,
            )
            completed_aspects = apply_compatibility_aspects(category_id, completed_aspects, compatibility)
            sanitize_single_value_aspects(completed_aspects, log=print)
            opt_data["categoryId"] = category_id
            opt_data["aspects"] = completed_aspects
            opt_data["motorsCompatibility"] = serialize_compatibility_analysis(compatibility)
            product["optimization"] = opt_data
            
            ebay_client.create_or_replace_inventory_item(
                sku=sku,
                product={
                    "title": title,
                    "description": description,
                    "image_urls": product.get('images', [])[:24],
                    "price": final_price,
                    "quantity": 1,  # Always 1 to avoid listing limits
                    "condition": "NEW",
                    "aspects": completed_aspects
                }
            )
            if compatibility.compatible_products:
                ebay_client.create_or_replace_product_compatibility(sku, compatibility.compatible_products)
            elif category_id in EBAY_MOTORS_CATEGORIES:
                ebay_client.delete_product_compatibility(sku)
            
            # 2. Create Offer with auto-matched category
            marketplace_id = "EBAY_MOTORS" if category_id in EBAY_MOTORS_CATEGORIES else None
            offer = ebay_client.create_offer(
                sku=sku,
                price=final_price,
                category_id=category_id,
                marketplace_id=marketplace_id
            )
            
            if not offer or not offer.get("offerId"):
                return {"status": "error", "message": "创建 Offer 失败"}
            
            offer_id = offer["offerId"]
            
            # 3. Publish to make it live
            publish_result = ebay_client.publish_offer(offer_id)
            listing_id = publish_result.get("listingId")
            
            if listing_id:
                _sync_live_category(opt_data, listing_id, category_id, category_name)
                product["optimization"] = opt_data
                # 4. Try to upload video if available
                videos = product.get('videos', [])
                if videos and len(videos) > 0:
                    try:
                        from src.services.ebay_video_uploader import EbayVideoUploader
                        video_uploader = EbayVideoUploader(oauth)
                        video_title = opt_data.get("title", product.get('title', ''))[:50]
                        video_id = video_uploader.upload_video_sync(videos[0], sku, video_title)
                        if video_id:
                            print(f"[Video] Uploaded: {video_id}")
                    except Exception as video_err:
                        print(f"[Video] Upload failed: {video_err}")
                
                return {
                    "status": "success",
                    "message": f"产品已发布! Listing ID: {listing_id}",
                    "listing_id": listing_id,
                    "offer_id": offer_id,
                    "category_id": category_id
                }
            else:
                return {
                    "status": "success",
                    "message": "产品已保存为草稿",
                    "offer_id": offer_id
                }
                
        except Exception as e:
            error_msg = str(e)
            
            # 检查是否是可重试的错误
            if attempt < max_retries - 1:
                fixed = try_fix_publish_error(
                    error_msg,
                    completed_aspects,
                    category_id,
                    EbayPublisher.CATEGORY_REQUIRED_ASPECTS,
                    MEASUREMENT_ASPECT_KEYS,
                    log=print,
                )
                if fixed:
                    if 'optimization' not in product or not isinstance(product['optimization'], dict):
                        product['optimization'] = {}
                    product['optimization']['aspects'] = completed_aspects
                    print(f"[重试 {attempt + 1}/{max_retries}] 已应用属性自动修复，准备重试")
                    continue

                if is_invalid_category_error(error_msg):
                    remapped = remap_legacy_category_id(category_id)
                    if remapped and remapped != category_id and is_sellable_leaf_category(oauth, remapped):
                        print(f"[AUTO-FIX] 类目重映射: {category_id} -> {remapped}")
                        category_id = remapped
                        if 'optimization' not in product or not isinstance(product['optimization'], dict):
                            product['optimization'] = {}
                        product['optimization']['categoryId'] = remapped
                        continue
            
            import traceback
            traceback.print_exc()
            return {"status": "error", "message": error_msg}


def delist_from_ebay(product: dict) -> dict:
    """从eBay下架产品 (withdraw offers → delete offers → delete inventory item)"""
    from src.clients.real_ebay_client import RealEbayClient
    from src.services.ebay_policy_manager import EbayPolicyManager
    
    sku = product['sku']
    environment = os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
    oauth = get_oauth_service()
    
    if not oauth.is_authorized():
        return {"status": "error", "message": "eBay 未授权，请先完成授权"}
    
    try:
        policy_manager = EbayPolicyManager(oauth)
        ebay_client = RealEbayClient(oauth, policy_manager)
        
        result = ebay_client.delist_sku(sku)
        
        if result["success"]:
            # Reset product in DB
            product['status'] = 'PENDING'
            product['listing_id'] = None
            product['optimization'] = None
            product['suggested_price'] = None
            product['cost_breakdown'] = None
            product['published_at'] = None
            product['logs'] = None
            save_product(product)
            
            return {
                "status": "success",
                "message": f"SKU {sku} 已从eBay下架并重置为 PENDING",
                "steps": result.get("steps", [])
            }
        else:
            return {
                "status": "error",
                "message": f"下架失败: {result.get('error', 'Unknown error')}",
                "steps": result.get("steps", [])
            }
    except Exception as e:
        return {"status": "error", "message": f"下架出错: {str(e)}"}


# ============================================================================
# Initialize
# ============================================================================

init_db()

# ============================================================================
# Plugin System - 加载插件
# ============================================================================

try:
    from src.plugins import load_plugins_from_directory, get_all_plugins, SharedServices
    
    # 加载所有插件
    load_plugins_from_directory()
    _plugins = get_all_plugins()
    
    # 创建共享服务
    def get_shared_services():
        """创建共享服务对象供插件使用"""
        return SharedServices(
            ebay_oauth=get_oauth_service(),
            db_path=get_db_path(),
            root_dir=str(root_dir),
            environment=os.getenv("EBAY_ENVIRONMENT", "PRODUCTION")
        )
    
    plugins_loaded = True
    print(f"Loaded {len(_plugins)} plugins: {', '.join([p.name for p in _plugins.values()])}")
except Exception as e:
    _plugins = {}
    plugins_loaded = False
    print(f"Plugin system not available: {e}")

# ============================================================================
# Sidebar Navigation
# ============================================================================

st.sidebar.title("🛍️ Dajian Listing Tool")
st.sidebar.markdown("---")

# 主应用页面
main_pages = ["🏠 首页", "📊 Dashboard", "📈 竞争监控", "🚦 CRO 状态", "🛡️ 守门员", "💰 财务", "🔐 eBay 授权", "📦 产品管理", "🚀 批量发布", "🎯 市场智能", "📦 库存管理", "⚙️ 设置"]

# 添加插件页面
plugin_pages = [f"{p.icon} {p.name}" for p in _plugins.values()] if _plugins else []

# 合并所有页面
if plugin_pages:
    all_pages = main_pages + ["---"] + plugin_pages
else:
    all_pages = main_pages

page = st.sidebar.radio(
    "导航",
    [p for p in all_pages if p != "---"],
    format_func=lambda x: x
)

st.sidebar.markdown("---")

# Status indicators
if check_authorization():
    st.sidebar.success("✅ eBay 已授权")
else:
    st.sidebar.error("❌ eBay 未授权")

env = os.getenv("EBAY_ENVIRONMENT", "NOT SET")
st.sidebar.info(f"环境: {env}")

st.sidebar.caption("v2.0.0 | 单一应用架构")

# ============================================================================
# Page: 首页
# ============================================================================

if page == "🏠 首页":
    st.title("🛍️ Dajian Listing Tool")
    st.markdown("---")
    
    col1, col2, col3 = st.columns(3)
    
    products = get_all_products()
    pending = len([p for p in products if p.get('status') == 'PENDING'])
    ready = len([p for p in products if p.get('status') in ['READY', 'READY_TO_PUBLISH']])
    published = len([p for p in products if p.get('status') == 'PUBLISHED'])
    
    with col1:
        st.metric("待处理", pending)
    with col2:
        st.metric("已优化", ready)
    with col3:
        st.metric("已发布", published)

    # ─── CRO 主线指标卡片 ────────────────────────────────────────────
    try:
        import sqlite3 as _sqlite3
        from datetime import date as _date, timedelta as _td
        from src.services.cro_action_queue import queue_stats as _cro_queue_stats
        _today = _date.today()
        _start = (_today - _td(days=6)).isoformat()
        with _sqlite3.connect('ebay_collection.db') as _c:
            _row = _c.execute(
                "SELECT AVG(cro_score), COUNT(DISTINCT sku) FROM cro_snapshots "
                "WHERE snapshot_date BETWEEN ? AND ?", (_start, _today.isoformat())
            ).fetchone()
            _avg_today = _c.execute(
                "SELECT AVG(cro_score) FROM cro_snapshots WHERE snapshot_date = ?",
                (_today.isoformat(),)
            ).fetchone()[0]
            _trend = _c.execute(
                "SELECT snapshot_date, AVG(cro_score) FROM cro_snapshots "
                "WHERE snapshot_date BETWEEN ? AND ? "
                "GROUP BY snapshot_date ORDER BY snapshot_date",
                (_start, _today.isoformat())
            ).fetchall()
            # S24 funnel breakdown 今日各阶段计数
            try:
                _funnel_rows = _c.execute(
                    "SELECT funnel_stage, COUNT(*) FROM cro_snapshots "
                    "WHERE snapshot_date = ? GROUP BY funnel_stage",
                    (_today.isoformat(),)
                ).fetchall()
            except Exception:
                _funnel_rows = []
        _qstats = _cro_queue_stats()
        _avg7d = float(_row[0]) if _row and _row[0] is not None else None
        _avg_t = float(_avg_today) if _avg_today is not None else None
        st.markdown("---")
        st.subheader("🎯 CRO 转化率主线 (北极星)")
        cc1, cc2, cc3, cc4 = st.columns(4)
        cc1.metric("今日 CRO 分", f"{_avg_t:.0f}/100" if _avg_t else "—")
        cc2.metric("7d 平均", f"{_avg7d:.0f}" if _avg7d else "—",
                   delta=f"{(_avg_t - _avg7d):+.1f}" if (_avg_t and _avg7d) else None)
        cc3.metric("P1 待办", _qstats.get('pending', 0))
        cc4.metric("已闭环", _qstats.get('done', 0))
        if _trend and len(_trend) >= 2:
            import pandas as _pd
            _df = _pd.DataFrame(_trend, columns=['date', 'avg_cro'])
            st.line_chart(_df.set_index('date'), height=140)

        # S24 — 今日 funnel 瀑布图
        if _funnel_rows:
            import pandas as _pd
            _stage_labels = {
                'no_impression': '🌑 零展示',
                'insufficient_data': '🌑 数据不足',
                'low_ctr': '🌒 低 CTR',
                'low_cvr': '🌓 低 CVR',
                'healthy': '🌕 健康',
            }
            _ordered = ['no_impression', 'insufficient_data',
                        'low_ctr', 'low_cvr', 'healthy']
            _stage_map = dict(_funnel_rows)
            _funnel_df = _pd.DataFrame([
                {'阶段': _stage_labels.get(k, k),
                 '数量': int(_stage_map.get(k, 0))}
                for k in _ordered if _stage_map.get(k, 0) > 0
            ])
            if not _funnel_df.empty:
                st.markdown("**今日漏斗分布**")
                st.bar_chart(_funnel_df.set_index('阶段'), height=160,
                             color='#FF8C00')
                _lc = int(_stage_map.get('low_ctr', 0))
                _lv = int(_stage_map.get('low_cvr', 0))
                _ni = int(_stage_map.get('no_impression', 0))
                if _lc or _lv or _ni:
                    st.caption(
                        f"零展示 {_ni} · low_ctr {_lc} · low_cvr {_lv} → "
                        "已自动入队 P1 (改价/换图/补 specifics/promote)"
                    )

        # 今日 sentinel 告警 (S18)
        try:
            import json as _json
            _sh = Path('logs/_scheduler_health.json')
            if _sh.exists():
                _data = _json.loads(_sh.read_text(encoding='utf-8'))
                _sent = (_data.get('tasks') or {}).get('cro_sentinel') or {}
                if _sent.get('status') == 'success' and _sent.get('at'):
                    from datetime import datetime as _dt
                    try:
                        _ran = _dt.fromisoformat(_sent['at']).date()
                    except Exception:
                        _ran = None
                    # 读 sentinel 输出文件
                    _alert_files = sorted(
                        Path('logs').glob('cro_sentinel_*.json'),
                        reverse=True,
                    )[:1]
                    if _alert_files and _ran == _date.today():
                        _rep = _json.loads(_alert_files[0].read_text(encoding='utf-8'))
                        if _rep.get('should_alert'):
                            _w = _rep.get('worsened') or []
                            _drop = _rep.get('drop', 0)
                            st.error(
                                f"🚨 今日 CRO 告警: 7d 跌 {_drop:.1f} 分 · "
                                f"恶化 {len(_w)}/{_rep.get('today_total',0)} SKU "
                                f"→ [打开「📈 竞争监控」](?page=竞争监控) 复盘"
                            )
                            if _w:
                                with st.expander(f"恶化 SKU 列表 ({len(_w)})"):
                                    st.code(', '.join(_w[:50]))
        except Exception:
            pass

        st.caption(
            "09:30 诊断 → 10:00 改价消费 → 10:15 图片刷新 → 10:30 北极星告警 · "
            "详情见「📈 竞争监控 → 🎯 转化率诊断」"
        )
    except Exception as _e:
        # 表不存在或还没数据时静默
        pass

    st.markdown("---")
    st.subheader("📋 快速指南")
    
    st.markdown("""
    ### 使用流程:
    
    1. **🔐 eBay 授权** - 首次使用需要授权 eBay 账号
    2. **📥 采集产品** - 使用浏览器扩展从大件云仓采集产品
    3. **🤖 AI 优化** - 自动优化标题、描述、定价
    4. **🚀 发布到 eBay** - 一键发布到 eBay
    
    ### 浏览器扩展配置:
    
    扩展已配置为发送数据到 **本地 FastAPI 服务器** (`http://localhost:8000`)。
    
    如需使用，请在本地运行:
    ```bash
    python server.py
    ```
    
    或者使用本应用的内置 API 接收功能（开发中）。
    """)

# ============================================================================
# Page: Dashboard (Review & Publish with Auto Category)
# ============================================================================

elif page == "📊 Dashboard":
    st.title("📊 Product Dashboard")
    st.markdown("Review products and publish to eBay - 查看和发布产品")
    
    # Refresh button
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 刷新列表", use_container_width=True):
            st.rerun()
    
    # Check eBay authorization
    if check_authorization():
        st.success("✅ eBay 已授权 - Ready to publish")
    else:
        st.warning("⚠️ eBay 未授权 - 请先在 eBay 授权页面完成授权")

    render_latest_reprice_panel()
    
    products = get_all_products()
    
    if not products:
        st.info("📭 No products collected yet. Use browser extension to collect products.")
        st.stop()
    
    # ===== SKU SEARCH BAR =====
    search_query = st.text_input("🔍 搜索 SKU", placeholder="输入 SKU 搜索...", key="dashboard_sku_search")
    if search_query:
        search_query_upper = search_query.strip().upper()
        products = [p for p in products if search_query_upper in p.get('sku', '').upper()]
        if not products:
            st.warning(f"未找到包含 '{search_query.strip()}' 的 SKU")
            st.stop()
        st.caption(f"找到 {len(products)} 条匹配记录")
    
    # ===== CHECK REVIEW MODE FIRST =====
    if 'review_sku' in st.session_state:
        review_sku = st.session_state['review_sku']
        review_product = get_product(review_sku)
        
        if review_product:
            st.markdown("---")
            st.subheader(f"📝 Review & Publish: {review_sku}")
            
            opt = review_product.get('optimization', {})
            if isinstance(opt, str):
                opt = {"title": review_product.get('title', ''), "description": opt}
            if not isinstance(opt, dict):
                opt = {}
            
            if opt:
                # Show category
                cat_display = get_category_display(review_product)
                st.info(f"📂 类目: {cat_display}")
                if review_product.get('status') in ['READY', 'READY_TO_PUBLISH'] and is_mi_draft_product(review_product):
                    st.caption("🧠 MI 机会草稿 · 已由后台自动起草，仍需人工审核后再发布")
                
                # Editable title
                new_title = st.text_input("标题 (Title)", value=opt.get('title', review_product.get('title', '')), key="review_title_input")
                
                # Description preview
                description = opt.get('description', review_product.get('description', ''))
                
                with st.expander("📝 描述预览 (Description)", expanded=True):
                    st.markdown(description, unsafe_allow_html=True)
                
                # Regenerate button
                if st.button("🔄 重新生成描述", key="regen_in_review", type="secondary"):
                    with st.spinner("AI 重新生成中..."):
                        new_optimization = run_ai_optimization(review_product)
                        review_product['optimization'] = new_optimization
                        save_product(review_product)
                        st.success("✅ 已重新生成!")
                        st.rerun()
                
                # Item Specifics
                with st.expander("🏷️ Item Specifics", expanded=False):
                    aspects = opt.get('aspects', {})
                    if aspects:
                        for k, v in aspects.items():
                            st.write(f"- **{k}**: {v}")
                    else:
                        st.write("No aspects available")
                
                # Price
                st.write(f"**建议售价**: ${review_product.get('suggested_price', 0):.2f}")
                
                # Pre-publish QC
                qc_issues = pre_publish_qc(review_product)
                if qc_issues:
                    with st.expander(f"⚠️ 发布前质检 ({len(qc_issues)} 项)", expanded=True):
                        for issue in qc_issues:
                            st.write(issue)
                else:
                    st.success("✅ 质检通过 — 可以发布")
                
                # Actions
                col1, col2, col3 = st.columns(3)
                with col1:
                    if st.button("❌ 取消", use_container_width=True, key="review_cancel"):
                        del st.session_state['review_sku']
                        st.rerun()
                with col2:
                    if st.button("💾 保存修改", use_container_width=True, key="review_save"):
                        opt['title'] = new_title
                        review_product['optimization'] = opt
                        save_product(review_product)
                        st.success("✅ 已保存!")
                        del st.session_state['review_sku']
                        st.rerun()
                with col3:
                    if st.button("🚀 确认发布到 eBay", type="primary", use_container_width=True, key="review_publish"):
                        # Block publish if critical QC issues exist
                        critical = [i for i in qc_issues if i.startswith("❌")]
                        if critical:
                            st.error("❌ 有关键质检问题未解决，无法发布:")
                            for c in critical:
                                st.write(c)
                        elif not check_authorization():
                            st.error("❌ 请先完成 eBay 授权!")
                        else:
                            with st.spinner("发布中..."):
                                opt['title'] = new_title
                                review_product['optimization'] = opt
                                save_product(review_product)
                                
                                # Use unified publisher with auto category
                                result = publish_with_auto_category(review_product)
                                if result['status'] == 'success':
                                    review_product['status'] = 'PUBLISHED' if result.get('listing_id') else 'READY_TO_PUBLISH'
                                    review_product['listing_id'] = result.get('listing_id') or result.get('offer_id')
                                    save_product(review_product)
                                    st.success(f"✅ {result['message']}")
                                    del st.session_state['review_sku']
                                    st.rerun()
                                else:
                                    st.error(f"❌ {result['message']}")
            else:
                st.warning("产品尚未优化，请先运行 AI 优化")
                if st.button("❌ 返回", key="review_back"):
                    del st.session_state['review_sku']
                    st.rerun()
        else:
            st.error("产品不存在")
            del st.session_state['review_sku']
            st.rerun()
        
        st.stop()  # Don't show tabs when in review mode
    
    # Filter tabs
    tab1, tab2, tab3, tab4 = st.tabs(["📋 All", "⏳ Pending", "✅ Ready", "🎉 Published"])
    
    def render_product_card(product, container, tab_prefix=""):
        """Render a product card with review and publish options"""
        sku = product['sku']
        status = product.get('status', 'PENDING')
        key_prefix = f"{tab_prefix}_{sku}" if tab_prefix else sku
        
        with container:
            status_colors = {'PENDING': '🟡', 'READY': '🔵', 'READY_TO_PUBLISH': '🟢', 'PUBLISHED': '✅', 'ERROR': '🔴'}
            
            col1, col2, col3 = st.columns([1, 2, 1])
            
            with col1:
                images = product.get('images', [])
                if images:
                    st.image(images[0], width=120)
                else:
                    st.write("📷 No image")
            
            with col2:
                st.write(f"**{status_colors.get(status, '⚪')} {status}**")
                opt = product.get('optimization', {})
                if isinstance(opt, dict) and opt.get('title'):
                    title = opt.get('title')
                else:
                    title = product.get('title', 'No Title')
                st.write(f"**{title[:60]}...**" if len(title) > 60 else f"**{title}**")
                st.caption(f"SKU: {sku}")
                if status in ['READY', 'READY_TO_PUBLISH'] and is_mi_draft_product(product):
                    st.caption("🧠 MI 机会草稿")
                
                # Show category
                cat_display = get_category_display(product)
                st.caption(f"📂 类目: {cat_display}")
                
                price_col1, price_col2 = st.columns(2)
                with price_col1:
                    st.write(f"成本: ${product.get('price', 0):.2f}")
                with price_col2:
                    if product.get('suggested_price'):
                        st.write(f"售价: **${product.get('suggested_price'):.2f}**")
                
                if product.get('listing_id') and status == 'PUBLISHED':
                    listing_id = product['listing_id']
                    st.markdown(f"[🔗 View on eBay](https://www.ebay.com/itm/{listing_id})")
            
            with col3:
                if status == 'PENDING':
                    if st.button("🤖 AI 优化", key=f"dash_opt_{key_prefix}", use_container_width=True):
                        with st.spinner("优化中..."):
                            pricing = calculate_pricing(product)
                            product['cost_breakdown'] = pricing['cost_breakdown']
                            product['suggested_price'] = pricing['suggested_price']
                            optimization = run_ai_optimization(product)
                            product['optimization'] = optimization
                            product['status'] = 'READY'
                            save_product(product)
                            st.success("✅ 优化完成!")
                            st.rerun()
                
                elif status in ['READY', 'READY_TO_PUBLISH']:
                    if st.button("📝 Review & Publish", key=f"dash_review_{key_prefix}", use_container_width=True):
                        st.session_state['review_sku'] = sku
                        st.rerun()
                    
                    if st.button("🔄 重新生成", key=f"dash_regen_{key_prefix}", use_container_width=True):
                        with st.spinner("重新生成中..."):
                            optimization = run_ai_optimization(product)
                            product['optimization'] = optimization
                            save_product(product)
                            st.success("✅ 描述已重新生成!")
                            st.rerun()
                
                elif status == 'PUBLISHED':
                    st.success("已发布 ✅")
                    if st.button("🔄 下架重新采集", key=f"dash_delist_{key_prefix}", use_container_width=True):
                        with st.spinner("正在从eBay下架..."):
                            result = delist_from_ebay(product)
                            if result['status'] == 'success':
                                st.success(f"✅ {result['message']}")
                                st.rerun()
                            else:
                                st.error(f"❌ {result['message']}")
                                for step in result.get('steps', []):
                                    st.caption(step)
            
            st.markdown("---")
    
    with tab1:
        for p in products:
            render_product_card(p, st.container(), "all")
    
    with tab2:
        pending = [p for p in products if p.get('status') == 'PENDING']
        if pending:
            # 批量优化按钮
            st.markdown("### 批量操作")
            if st.button("🤖 批量优化所有 PENDING 产品", type="primary", use_container_width=True, key="batch_optimize_all"):
                progress = st.progress(0)
                status_text = st.empty()
                success_count = 0
                fail_count = 0
                
                for i, product in enumerate(pending):
                    sku = product['sku']
                    status_text.text(f"正在优化: {sku} ({i+1}/{len(pending)})...")
                    try:
                        # Calculate pricing
                        pricing = calculate_pricing(product)
                        product['cost_breakdown'] = pricing['cost_breakdown']
                        product['suggested_price'] = pricing['suggested_price']
                        
                        # Run AI optimization
                        optimization = run_ai_optimization(product)
                        product['optimization'] = optimization
                        product['status'] = 'READY'
                        save_product(product)
                        success_count += 1
                    except Exception as e:
                        fail_count += 1
                        st.warning(f"⚠️ {sku} 优化失败: {str(e)}")
                    
                    progress.progress((i + 1) / len(pending))
                
                status_text.empty()
                st.success(f"✅ 批量优化完成! 成功: {success_count}, 失败: {fail_count}")
                st.rerun()
            
            st.markdown("---")
            for p in pending:
                render_product_card(p, st.container(), "pending")
        else:
            st.info("No pending products")
    
    with tab3:
        ready = [p for p in products if p.get('status') in ['READY', 'READY_TO_PUBLISH']]
        if ready:
            for p in ready:
                render_product_card(p, st.container(), "ready")
        else:
            st.info("No ready products")
    
    with tab4:
        published = [p for p in products if p.get('status') == 'PUBLISHED']
        if published:
            st.markdown(f"### 已发布产品 ({len(published)})")
            
            # 批量下架（需要选择）
            with st.expander("⚠️ 批量下架操作", expanded=False):
                st.warning("批量下架将从eBay移除所有已发布的listing并重置为PENDING状态，可重新采集和发布。")
                confirm_text = st.text_input("输入 'DELIST ALL' 确认批量下架", key="batch_delist_confirm")
                if st.button("🗑️ 批量下架所有已发布产品", type="primary", key="batch_delist_all"):
                    if confirm_text == "DELIST ALL":
                        progress = st.progress(0)
                        status_text = st.empty()
                        success_count = 0
                        fail_count = 0
                        
                        for i, product in enumerate(published):
                            sku = product['sku']
                            status_text.text(f"正在下架: {sku} ({i+1}/{len(published)})...")
                            try:
                                result = delist_from_ebay(product)
                                if result['status'] == 'success':
                                    success_count += 1
                                else:
                                    fail_count += 1
                                    st.warning(f"⚠️ {sku}: {result['message']}")
                            except Exception as e:
                                fail_count += 1
                                st.warning(f"⚠️ {sku} 下架失败: {str(e)}")
                            
                            progress.progress((i + 1) / len(published))
                        
                        status_text.empty()
                        st.success(f"✅ 批量下架完成! 成功: {success_count}, 失败: {fail_count}")
                        st.rerun()
                    else:
                        st.error("请输入 'DELIST ALL' 确认操作")
            
            st.markdown("---")
            for p in published:
                render_product_card(p, st.container(), "published")
        else:
            st.info("No published products")

# ============================================================================
# Page: eBay 授权
# ============================================================================

elif page == "🔐 eBay 授权":
    st.title("🔐 eBay OAuth 授权")
    
    # Get query parameters (for OAuth callback)
    query_params = st.query_params
    
    # Handle OAuth callback
    if "code" in query_params or "ebayktn" in query_params:
        st.subheader("🔄 处理授权...")
        
        auth_code = query_params.get("code") or query_params.get("ebayktn")
        error = query_params.get("error")
        
        if error:
            st.error(f"❌ 授权失败: {error}")
        else:
            try:
                oauth = get_oauth_service()
                token_data = oauth.exchange_code_for_token(auth_code)
                st.success("✅ 授权成功！Token 已保存")
                st.query_params.clear()
                st.rerun()
            except Exception as e:
                st.error(f"❌ Token 交换失败: {str(e)}")
    
    else:
        # Authorization status
        is_authorized = check_authorization()
        
        if is_authorized:
            st.success("✅ 已授权")
            st.write("你的 eBay 账号已经授权，可以开始发布产品了！")
            
            # Token 导出功能
            st.markdown("---")
            st.subheader("📤 导出 Token（用于本地）")
            st.write("点击下载 Token 文件，然后在本地导入使用")
            
            try:
                oauth = get_oauth_service()
                token_data = oauth._get_stored_token()
                if token_data:
                    token_json = json.dumps(token_data, indent=2)
                    st.download_button(
                        label="⬇️ 下载 Token 文件",
                        data=token_json,
                        file_name="ebay_token.json",
                        mime="application/json",
                        type="primary"
                    )
                    st.info("💡 下载后，在本地 Streamlit 的设置页面导入此文件")
            except Exception as e:
                st.error(f"获取 Token 失败: {e}")
                
        else:
            st.warning("⚠️ 未授权")
            st.write("点击下面的按钮授权你的 eBay 账号")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🔐 开始授权", type="primary", use_container_width=True):
                try:
                    oauth = get_oauth_service()
                    auth_url = oauth.get_authorization_url(state="streamlit_auth")
                    st.markdown(f"[点击这里跳转到 eBay 授权页面]({auth_url})")
                    st.info("授权完成后，页面会自动返回")
                except Exception as e:
                    st.error(f"❌ 生成授权 URL 失败: {str(e)}")
        
        with col2:
            if st.button("🔄 刷新状态", use_container_width=True):
                st.rerun()
        
        # Show config
        st.markdown("---")
        st.subheader("⚙️ 当前配置")
        st.write(f"- **环境**: {os.getenv('EBAY_ENVIRONMENT', 'NOT SET')}")
        st.write(f"- **Redirect URI**: {os.getenv('EBAY_REDIRECT_URI', 'NOT SET')}")
        st.write(f"- **App ID**: {os.getenv('EBAY_APP_ID', 'NOT SET')[:20]}...")

# ============================================================================
# Page: 产品管理
# ============================================================================

elif page == "📦 产品管理":
    st.title("📦 产品管理")
    
    # Refresh button
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 刷新", use_container_width=True):
            st.rerun()
    
    products = get_all_products()
    
    if not products:
        st.info("📭 暂无产品。请使用浏览器扩展采集产品。")
    else:
        # Filter
        status_filter = st.selectbox(
            "筛选状态",
            ["全部", "PENDING", "READY", "READY_TO_PUBLISH", "PUBLISHED", "ERROR"]
        )
        
        if status_filter != "全部":
            products = [p for p in products if p.get('status') == status_filter]
        
        st.write(f"共 {len(products)} 个产品")
        
        for product in products:
            with st.expander(f"**{product['sku']}** - {product.get('title', 'No Title')[:50]}...", expanded=False):
                col1, col2, col3 = st.columns([2, 1, 1])
                
                with col1:
                    st.write(f"**状态**: {product.get('status', 'UNKNOWN')}")
                    st.write(f"**大建价格**: ${product.get('price', 0):.2f}")
                    st.write(f"**运费**: ${product.get('shipping', 0):.2f}")
                    
                    if product.get('suggested_price'):
                        st.write(f"**建议售价**: ${product.get('suggested_price'):.2f}")
                    
                    if product.get('cost_breakdown'):
                        with st.popover("💰 成本明细"):
                            st.json(product['cost_breakdown'])
                
                with col2:
                    # Images
                    images = product.get('images', [])
                    if images:
                        st.image(images[0], width=150)
                
                with col3:
                    # Actions
                    sku = product['sku']
                    
                    if st.button("🤖 AI 优化", key=f"opt_{sku}", use_container_width=True):
                        with st.spinner("优化中..."):
                            # Calculate pricing
                            pricing = calculate_pricing(product)
                            product['cost_breakdown'] = pricing['cost_breakdown']
                            product['suggested_price'] = pricing['suggested_price']
                            
                            # Run AI optimization
                            optimization = run_ai_optimization(product)
                            product['optimization'] = optimization
                            product['status'] = 'READY'
                            
                            save_product(product)
                            st.success("✅ 优化完成")
                            st.rerun()
                    
                    if product.get('status') in ['READY', 'READY_TO_PUBLISH']:
                        if st.button("🚀 发布", key=f"pub_{sku}", use_container_width=True):
                            with st.spinner("发布中..."):
                                result = publish_with_auto_category(product)
                                if result['status'] == 'success':
                                    product['status'] = 'PUBLISHED' if result.get('listing_id') else 'READY_TO_PUBLISH'
                                    product['listing_id'] = result.get('listing_id') or result.get('offer_id')
                                    save_product(product)
                                    st.success(f"✅ {result['message']}")
                                else:
                                    st.error(f"❌ {result['message']}")
                    
                    if st.button("🗑️ 删除", key=f"del_{sku}", use_container_width=True):
                        delete_product(sku)
                        st.success("已删除")
                        st.rerun()
                
                # Show optimization if available
                if product.get('optimization'):
                    with st.popover("📝 优化结果"):
                        opt = product['optimization']
                        # Handle both dict and string formats
                        if isinstance(opt, dict):
                            st.write(f"**优化标题**: {opt.get('title', 'N/A')}")
                            if opt.get('aspects'):
                                st.write("**Item Specifics**:")
                                st.json(opt['aspects'])
                        else:
                            st.write(f"**优化结果**: {str(opt)[:500]}")

# ============================================================================
# Page: 批量发布
# ============================================================================

elif page == "🚀 批量发布":
    st.title("🚀 批量发布")
    
    if not check_authorization():
        st.error("❌ 请先完成 eBay 授权")
        st.stop()
    
    products = get_all_products()
    ready_products = [p for p in products if p.get('status') in ['READY', 'READY_TO_PUBLISH']]
    
    if not ready_products:
        st.info("📭 没有已优化的产品。请先在产品管理中运行 AI 优化。")
    else:
        st.write(f"共 {len(ready_products)} 个产品待发布")
        
        # Preview
        for p in ready_products:
            st.write(f"- **{p['sku']}**: {p.get('title', '')[:50]}... - ${p.get('suggested_price', 0):.2f}")
        
        st.markdown("---")
        
        if st.button("🚀 批量发布所有", type="primary", use_container_width=True):
            progress = st.progress(0)
            status_text = st.empty()
            
            success_count = 0
            fail_count = 0
            skip_count = 0
            
            for i, product in enumerate(ready_products):
                sku = product['sku']
                status_text.text(f"发布中: {sku}...")
                
                # 预检查: 跳过无价格的产品
                opt_data = product.get('optimization', {})
                if isinstance(opt_data, str):
                    try:
                        opt_data = json.loads(opt_data)
                    except:
                        opt_data = {}
                
                final_price = product.get('suggested_price', 0) or product.get('price', 0)
                if not final_price or final_price <= 0:
                    skip_count += 1
                    st.warning(f"⏭️ {sku}: 价格为空，跳过")
                    progress.progress((i + 1) / len(ready_products))
                    continue
                
                # 使用自动类目匹配版本（避免 Invalid category 错误）
                result = publish_with_auto_category(product)
                
                if result['status'] == 'success':
                    listing_id = result.get('listing_id')
                    product['status'] = 'PUBLISHED' if listing_id else 'READY_TO_PUBLISH'
                    if listing_id:
                        product['listing_id'] = listing_id
                        success_count += 1
                    save_product(product)
                    st.success(f"✅ {product['sku']}: {result.get('message', '发布成功')}")
                else:
                    fail_count += 1
                    st.error(f"❌ {product['sku']}: {result.get('message', '发布失败')}")
                
                progress.progress((i + 1) / len(ready_products))
            
            status_text.empty()
            st.success(f"✅ 完成! 成功: {success_count}, 失败: {fail_count}, 跳过: {skip_count}")

# ============================================================================
# Page: 市场智能
# ============================================================================

elif page == "📈 竞争监控":
    try:
        from src.web.pages.competition_monitor import render_competition_monitor
        render_competition_monitor()
    except ImportError as e:
        st.error(f"竞争监控模块加载失败: {e}")
        st.info("请确保 src/web/pages/competition_monitor.py 文件存在")
    except Exception as e:
        st.error(f"页面渲染失败: {e}")
        import traceback
        st.code(traceback.format_exc())

elif page == "🚦 CRO 状态":
    try:
        from src.web.pages.cro_status import render_cro_status
        render_cro_status(set_page_config=False)
    except Exception as e:
        st.error(f"CRO 状态页加载失败: {e}")
        import traceback
        st.code(traceback.format_exc())

elif page == "🛡️ 守门员":
    try:
        from src.web.pages.pricing_guard_dashboard import render_pricing_guard_dashboard
        render_pricing_guard_dashboard()
    except Exception as e:
        st.error(f"守门员仪表盘加载失败: {e}")
        import traceback
        st.code(traceback.format_exc())

elif page == "💰 财务":
    try:
        from src.web.pages.finance_dashboard import render_finance_dashboard
        render_finance_dashboard()
    except Exception as e:
        st.error(f"财务页加载失败: {e}")
        import traceback
        st.code(traceback.format_exc())

elif page == "🎯 市场智能":
    try:
        from src.web.pages.market_intelligence import render_market_intelligence
        render_market_intelligence()
    except ImportError as e:
        st.error(f"市场智能模块加载失败: {e}")
        st.info("请确保 src/web/pages/market_intelligence.py 文件存在")
    except Exception as e:
        st.error(f"页面渲染失败: {e}")
        import traceback
        st.code(traceback.format_exc())

# ============================================================================
# Page: 库存管理
# ============================================================================

elif page == "📦 库存管理":
    try:
        from src.web.pages.inventory_management import render_inventory_management
        render_inventory_management()
    except ImportError as e:
        st.error(f"库存管理模块加载失败: {e}")
        st.info("请确保 src/web/pages/inventory_management.py 文件存在")
    except Exception as e:
        st.error(f"页面渲染失败: {e}")
        import traceback
        st.code(traceback.format_exc())

# ============================================================================
# Page: 设置
# ============================================================================

elif page == "⚙️ 设置":
    st.title("⚙️ 设置")
    
    # Token 导入功能
    st.subheader("📥 导入 eBay Token")
    st.write("从 Streamlit Cloud 下载的 Token 文件可以在这里导入")
    
    uploaded_file = st.file_uploader("选择 Token 文件 (ebay_token.json)", type=['json'])
    
    if uploaded_file is not None:
        try:
            token_data = json.loads(uploaded_file.read().decode('utf-8'))
            
            if 'access_token' in token_data:
                # 保存到本地数据库
                oauth = get_oauth_service()
                oauth._save_token(token_data)
                st.success("✅ Token 导入成功！")
                st.rerun()
            else:
                st.error("❌ 无效的 Token 文件")
        except Exception as e:
            st.error(f"❌ 导入失败: {e}")
    
    st.markdown("---")
    st.subheader("环境变量")
    
    env_vars = {
        "EBAY_ENVIRONMENT": os.getenv("EBAY_ENVIRONMENT", "NOT SET"),
        "EBAY_APP_ID": os.getenv("EBAY_APP_ID", "NOT SET")[:20] + "..." if os.getenv("EBAY_APP_ID") else "NOT SET",
        "EBAY_REDIRECT_URI": os.getenv("EBAY_REDIRECT_URI", "NOT SET"),
        "QWEN_API_KEY": "已配置" if os.getenv("QWEN_API_KEY") else "未配置",
    }
    
    for key, value in env_vars.items():
        st.write(f"- **{key}**: `{value}`")
    
    st.markdown("---")
    st.subheader("数据库")
    
    st.write(f"- **路径**: `{get_db_path()}`")
    
    products = get_all_products()
    st.write(f"- **产品数量**: {len(products)}")
    
    if st.button("🗑️ 清空所有产品", type="secondary"):
        if st.checkbox("确认删除所有产品"):
            conn = sqlite3.connect(get_db_path())
            cursor = conn.cursor()
            cursor.execute("DELETE FROM collected_products")
            conn.commit()
            conn.close()
            st.success("已清空")
            st.rerun()
    
    st.markdown("---")
    st.subheader("部署说明")
    
    st.markdown("""
    ### Streamlit Cloud 部署步骤:
    
    1. 推送代码到 GitHub
    2. 访问 [streamlit.io/cloud](https://streamlit.io/cloud)
    3. 点击 "New app" → 选择你的仓库
    4. 主文件路径: `app.py`
    5. 点击 Deploy
    
    ### 配置 Secrets:
    
    在 Streamlit Cloud 的 Settings → Secrets 中添加:
    
    ```toml
    EBAY_APP_ID = "your-app-id"
    EBAY_CERT_ID = "your-cert-id"
    EBAY_REDIRECT_URI = "https://your-app.streamlit.app"
    EBAY_ENVIRONMENT = "PRODUCTION"
    QWEN_API_KEY = "your-qwen-key"
    ```
    """)

# ============================================================================
# Plugin Pages - 渲染插件页面
# ============================================================================

# 检查是否选择了插件页面
if plugins_loaded and _plugins:
    for plugin in _plugins.values():
        plugin_page_name = f"{plugin.icon} {plugin.name}"
        if page == plugin_page_name:
            try:
                shared_services = get_shared_services()
                plugin.on_load(shared_services)
                plugin.render(shared_services)
            except Exception as e:
                st.error(f"插件加载失败: {plugin.name}")
                st.exception(e)
            break

# ============================================================================
# Footer
# ============================================================================

st.markdown("---")
st.caption("🔒 Dajian Listing Tool v2.1 | 支持插件扩展 | Streamlit Cloud 部署")
