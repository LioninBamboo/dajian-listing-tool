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
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Add root to path for imports
root_dir = Path(__file__).parent
sys.path.insert(0, str(root_dir))

# ============================================================================
# Page Config
# ============================================================================
st.set_page_config(
    page_title="Dajian Listing Tool",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded"
)

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

def save_product(data: dict):
    """Save or update product"""
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    
    # Serialize JSON fields
    for field in ['images', 'videos', 'attributes', 'specs', 'cost_breakdown', 'optimization', 'logs']:
        if field in data and data[field] is not None:
            if not isinstance(data[field], str):
                data[field] = json.dumps(data[field], ensure_ascii=False)
    
    now = datetime.utcnow().isoformat()
    
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
    """Calculate pricing for a product"""
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
    safe_price = PricingEngine.calculate_selling_price(dajian_costs["total_dajian_cost"], 0.15)
    min_price = PricingEngine.calculate_selling_price(dajian_costs["total_dajian_cost"], 0.08)
    
    decision = PricingEngine.determine_final_price(safe_price, min_price, None)
    
    return {
        "cost_breakdown": dajian_costs,
        "suggested_price": decision["price"],
        "safe_price": safe_price,
        "min_price": min_price,
        "strategy": decision["strategy"]
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
            "title": product.get('title', '')[:80],
            "description": product.get('description', ''),
            "aspects": {"Brand": ["AquaVerve"]},
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
            "title": product.get('title', '')[:80],
            "description": product.get('description', ''),
            "aspects": {"Brand": ["AquaVerve"]},
            "error": str(e)
        }

# ============================================================================
# eBay Publishing
# ============================================================================

def publish_to_ebay(product: dict) -> dict:
    """Publish product to eBay"""
    from src.clients.real_ebay_client import RealEbayClient
    from src.services.ebay_policy_manager import EbayPolicyManager
    
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
        
        # 1. Create Inventory Item
        ebay_client.create_or_replace_inventory_item(
            sku=sku,
            product={
                "title": opt_data.get("title", product.get('title', ''))[:80],
                "description": opt_data.get("description", product.get('description', '')),
                "image_urls": product.get('images', [])[:12],
                "price": final_price,
                "quantity": product.get('stock', 1) or 1,
                "condition": "NEW",
                "aspects": opt_data.get("aspects", {"Brand": ["AquaVerve"]})
            }
        )
        
        # 2. Create Offer
        category_id = opt_data.get("categoryId")
        offer = ebay_client.create_offer(
            sku=sku,
            price=final_price,
            category_id=category_id
        )
        
        if offer and offer.get("offerId"):
            return {
                "status": "success",
                "message": "产品已保存为草稿 (Offer 已创建)",
                "offer_id": offer["offerId"]
            }
        else:
            return {"status": "error", "message": "创建 Offer 失败"}
            
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ============================================================================
# Initialize
# ============================================================================

init_db()

# ============================================================================
# Sidebar Navigation
# ============================================================================

st.sidebar.title("🛍️ Dajian Listing Tool")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "导航",
    ["🏠 首页", "🔐 eBay 授权", "📦 产品管理", "🚀 批量发布", "⚙️ 设置"]
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
                                result = publish_to_ebay(product)
                                if result['status'] == 'success':
                                    product['status'] = 'READY_TO_PUBLISH'
                                    product['listing_id'] = result.get('offer_id')
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
            
            for i, product in enumerate(ready_products):
                status_text.text(f"发布中: {product['sku']}...")
                
                result = publish_to_ebay(product)
                
                if result['status'] == 'success':
                    success_count += 1
                    product['status'] = 'READY_TO_PUBLISH'
                    product['listing_id'] = result.get('offer_id')
                    save_product(product)
                else:
                    fail_count += 1
                
                progress.progress((i + 1) / len(ready_products))
            
            status_text.empty()
            st.success(f"✅ 完成! 成功: {success_count}, 失败: {fail_count}")

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
# Footer
# ============================================================================

st.markdown("---")
st.caption("🔒 Dajian Listing Tool v2.0 | 单一 Streamlit 应用架构 | 支持 Streamlit Cloud 部署")
