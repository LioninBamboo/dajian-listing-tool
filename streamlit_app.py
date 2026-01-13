"""
Streamlit App for Dajian Listing Tool

Features:
1. eBay OAuth Authorization
2. Product Collection from Dajian
3. AI Optimization with Qwen
4. Publish to eBay as READY_TO_PUBLISH
"""

import streamlit as st
import os
import sys
import json
from dotenv import load_dotenv
from pathlib import Path
import requests
import time

# Load environment variables
load_dotenv()

# Add root to path for imports
root_dir = Path(__file__).parent
sys.path.insert(0, str(root_dir))

from src.services.ebay_auth import EbayOAuthService

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
# Helper Functions
# ============================================================================

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
# Sidebar Navigation
# ============================================================================

st.sidebar.title("🛍️ Dajian Listing Tool")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "导航",
    ["🔐 eBay 授权", "📦 产品发布", "📊 状态查看"]
)

st.sidebar.markdown("---")
st.sidebar.caption("v1.0.0 | Powered by Streamlit")

# ============================================================================
# Page 1: eBay Authorization
# ============================================================================

if page == "🔐 eBay 授权":
    st.title("🔐 eBay OAuth 授权")
    
    # Get query parameters
    query_params = st.query_params
    
    # Check if this is an OAuth callback
    if "code" in query_params or "ebayktn" in query_params:
        st.subheader("🔄 处理授权...")
        
        # Get the authorization code
        auth_code = query_params.get("code") or query_params.get("ebayktn")
        error = query_params.get("error")
        error_description = query_params.get("error_description")
        
        if error:
            st.error(f"❌ 授权失败")
            st.write(f"**错误**: {error}")
            st.write(f"**描述**: {error_description}")
        else:
            try:
                st.info(f"🔄 交换授权码...")
                
                oauth = get_oauth_service()
                token_data = oauth.exchange_code_for_token(auth_code)
                
                st.success("✅ 授权成功！")
                st.write("你的 eBay 账号已授权，现在可以：")
                st.write("- 创建和管理产品列表")
                st.write("- 上传库存项目")
                st.write("- 发布商品到 eBay")
                
                st.success("💾 Token 已保存")
                
            except Exception as e:
                st.error(f"❌ Token 交换失败")
                st.write(f"**错误**: {str(e)}")
    
    else:
        # Show authorization start page
        st.subheader("📝 开始授权")
        
        # Check current status
        is_authorized = check_authorization()
        
        if is_authorized:
            st.success("✅ 已授权")
            st.write("你的 eBay 账号已经授权，可以开始发布产品了！")
            
            # ============ Token 导出功能 ============
            st.markdown("---")
            st.subheader("📤 导出 Token（用于本地使用）")
            st.write("点击下载 Token 文件，然后在本地应用中导入")
            
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
                    st.info("💡 下载后，在本地 Streamlit 的 **⚙️ 设置** 页面导入此文件即可使用")
                else:
                    st.warning("Token 数据为空")
            except Exception as e:
                st.error(f"获取 Token 失败: {e}")
            # ============ Token 导出功能结束 ============
            
        else:
            st.warning("⚠️ 未授权")
            st.write("点击下面的按钮授权你的 eBay 账号：")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("🔐 授权 eBay", type="primary", use_container_width=True):
                try:
                    oauth = get_oauth_service()
                    auth_url = oauth.get_authorization_url(state="streamlit_auth")
                    
                    st.info("ℹ️ 跳转到 eBay...")
                    
                    # Use HTML to redirect
                    st.markdown(f"""
                        <script>
                        window.location.href = "{auth_url}";
                        </script>
                    """, unsafe_allow_html=True)
                    
                except Exception as e:
                    st.error(f"❌ 生成授权 URL 失败: {str(e)}")
        
        with col2:
            if st.button("ℹ️ 检查状态", use_container_width=True):
                if check_authorization():
                    st.success("✅ 已授权")
                else:
                    st.warning("⚠️ 未授权")

# ============================================================================
# Page 2: Product Publishing
# ============================================================================

elif page == "📦 产品发布":
    st.title("📦 产品发布")
    
    # Check authorization first
    if not check_authorization():
        st.error("❌ 未授权")
        st.write("请先在 '🔐 eBay 授权' 页面完成授权")
        st.stop()
    
    st.success("✅ eBay 已授权")
    
    # Product SKU input
    st.subheader("1️⃣ 输入产品 SKU")
    sku = st.text_input("SKU", placeholder="例如: TOOL-12345")
    
    if st.button("🚀 开始发布", type="primary", disabled=not sku):
        with st.spinner("处理中..."):
            # Create progress container
            progress_container = st.container()
            
            with progress_container:
                # Step 1: Collect Product
                st.write("### [1/3] 📥 采集产品")
                try:
                    # Call collection API
                    response = requests.post(
                        "http://localhost:8000/api/collect",
                        json={"sku": sku}
                    )
                    
                    if response.status_code == 200:
                        st.success("✅ 产品采集成功")
                        result = response.json()
                        st.json(result)
                    else:
                        st.error(f"❌ 采集失败: {response.status_code}")
                        st.write(response.text)
                        st.stop()
                except Exception as e:
                    st.error(f"❌ 采集失败: {str(e)}")
                    st.write("请确保 FastAPI 服务器正在运行: `python server.py`")
                    st.stop()
                
                # Step 2: Wait for AI Optimization
                st.write("### [2/3] 🤖 AI 优化")
                with st.spinner("等待 Qwen AI 优化..."):
                    max_attempts = 30
                    for i in range(max_attempts):
                        time.sleep(2)
                        
                        # Check status
                        try:
                            status_response = requests.get(
                                f"http://localhost:8000/api/status/{sku}"
                            )
                            
                            if status_response.status_code == 200:
                                status_data = status_response.json()
                                
                                if status_data.get("status") == "OPTIMIZED":
                                    st.success("✅ AI 优化完成")
                                    break
                                elif status_data.get("status") == "ERROR":
                                    st.error("❌ 优化失败")
                                    st.write(status_data.get("error"))
                                    st.stop()
                        except:
                            pass
                        
                        if i == max_attempts - 1:
                            st.error("❌ 优化超时")
                            st.stop()
                
                # Step 3: Publish to eBay
                st.write("### [3/3] 📤 发布到 eBay")
                try:
                    publish_response = requests.post(
                        "http://localhost:8000/api/publish",
                        json={"sku": sku, "mode": "READY_TO_PUBLISH"}
                    )
                    
                    if publish_response.status_code == 200:
                        st.success("✅ 发布成功！")
                        publish_data = publish_response.json()
                        st.json(publish_data)
                        
                        st.balloons()
                    else:
                        st.error(f"❌ 发布失败: {publish_response.status_code}")
                        st.write(publish_response.text)
                except Exception as e:
                    st.error(f"❌ 发布失败: {str(e)}")

# ============================================================================
# Page 3: Status Dashboard
# ============================================================================

elif page == "📊 状态查看":
    st.title("📊 状态查看")
    
    st.subheader("eBay 授权状态")
    if check_authorization():
        st.success("✅ 已授权")
    else:
        st.error("❌ 未授权")
    
    st.subheader("环境配置")
    st.write(f"- **环境**: {os.getenv('EBAY_ENVIRONMENT', 'NOT SET')}")
    st.write(f"- **Redirect URI**: {os.getenv('EBAY_REDIRECT_URI', 'NOT SET')}")
    
    st.subheader("API 端点")
    st.code("""
    POST /api/collect - 采集产品
    GET  /api/status/{sku} - 查询状态
    POST /api/publish - 发布到 eBay
    """)

# ============================================================================
# Footer
# ============================================================================
st.markdown("---")
st.caption("🔒 此应用安全处理 eBay OAuth 回调。不存储个人数据。")
