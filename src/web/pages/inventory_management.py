"""
库存管理页面 - Streamlit UI

功能:
1. 查看已刊登产品的库存状态
2. 手动/自动同步大建云仓库存
3. 库存预警和自动下架
4. 价格变动监控
"""
import streamlit as st
import pandas as pd
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")


def render_inventory_management():
    """渲染库存管理页面"""
    st.title("📦 库存管理")
    st.markdown("同步大建云仓库存，自动管理 eBay 刊登状态")
    
    # 标签页
    tab1, tab2, tab3 = st.tabs(["📊 库存概览", "🔄 同步历史", "⚙️ 设置"])
    
    with tab1:
        render_inventory_overview()
    
    with tab2:
        render_sync_history()
    
    with tab3:
        render_sync_settings()


def render_inventory_overview():
    """库存概览"""
    import sqlite3
    import json
    
    db_path = PROJECT_ROOT / "ebay_collection.db"
    
    if not db_path.exists():
        st.warning("数据库不存在")
        return
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    # 获取已刊登产品
    cur.execute("""
        SELECT sku, title, url, cost_breakdown, status, 
               listing_id, updated_at
        FROM collected_products 
        WHERE status IN ('PUBLISHED', 'READY', 'PENDING')
        ORDER BY updated_at DESC
    """)
    
    products = []
    for row in cur.fetchall():
        cost_data = json.loads(row['cost_breakdown']) if row['cost_breakdown'] else {}
        products.append({
            'SKU': row['sku'],
            '标题': row['title'][:40] + '...' if len(row['title'] or '') > 40 else row['title'],
            '大建成本': f"${cost_data.get('total_dajian_cost', 0):.2f}",
            '状态': row['status'],
            'eBay Listing': row['listing_id'] or '-',
            '更新时间': row['updated_at'][:10] if row['updated_at'] else '-',
            'url': row['url'],
            'sku': row['sku']
        })
    
    conn.close()
    
    if not products:
        st.info("暂无刊登产品")
        return
    
    # 统计卡片
    col1, col2, col3, col4 = st.columns(4)
    
    published = sum(1 for p in products if p['状态'] == 'PUBLISHED')
    ready = sum(1 for p in products if p['状态'] == 'READY')
    
    with col1:
        st.metric("已刊登", published)
    with col2:
        st.metric("待刊登", ready)
    with col3:
        st.metric("总计", len(products))
    with col4:
        st.metric("需检查库存", "?", help="运行同步后显示")
    
    st.divider()
    
    # 操作区
    col1, col2 = st.columns([3, 1])
    
    with col1:
        st.subheader("产品列表")
    
    with col2:
        # 同步数量选择
        sync_limit = st.selectbox("同步数量", [10, 50, 100, 0], format_func=lambda x: "全部" if x == 0 else f"前 {x} 个", key="sync_limit")
        
        # 跳过今日已同步
        skip_synced = st.checkbox("跳过今日已同步", value=True, help="避免重复同步同一产品")
        
        if st.button("🔄 立即同步库存", type="primary", use_container_width=True):
            with st.spinner(f"正在检查大建云仓库存 ({'全部' if sync_limit == 0 else f'前{sync_limit}个'})..."):
                sync_results = run_inventory_sync(limit=sync_limit, skip_synced_today=skip_synced)
                if sync_results:
                    checked = sync_results.get('checked', 0)
                    out_of_stock = sync_results.get('out_of_stock', [])
                    price_changed = sync_results.get('price_changed', [])
                    no_change = sync_results.get('no_change', 0)
                    skipped = sync_results.get('skipped', 0)
                    errors = sync_results.get('errors', 0)
                    skipped_today = sync_results.get('skipped_synced_today', 0)
                    
                    # 显示详细结果
                    col_a, col_b, col_c, col_d = st.columns(4)
                    col_a.metric("已检查", checked)
                    col_b.metric("缺货 (已设0)", len(out_of_stock), delta="eBay已更新" if out_of_stock else None, delta_color="inverse")
                    col_c.metric("价格变动", len(price_changed))
                    col_d.metric("今日已跳过", skipped_today, help="今日已同步过的SKU")
                    
                    if out_of_stock:
                        st.warning(f"⚠️ 缺货SKU (eBay库存已设为0): {', '.join(out_of_stock[:5])}{'...' if len(out_of_stock) > 5 else ''}")
                    if errors > 0:
                        st.info(f"ℹ️ {errors} 个产品无法获取大建库存信息 (可能不在收藏夹)")
                else:
                    st.error("同步失败")
    
    # 产品表格
    df = pd.DataFrame(products)
    display_cols = ['SKU', '标题', '大建成本', '状态', 'eBay Listing', '更新时间']
    
    st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "SKU": st.column_config.TextColumn("SKU", width="small"),
            "大建成本": st.column_config.TextColumn("大建成本", width="small"),
            "状态": st.column_config.TextColumn("状态", width="small"),
        }
    )


def render_sync_history():
    """同步历史"""
    st.subheader("同步历史记录")
    
    log_dir = PROJECT_ROOT / "logs"
    sync_logs = list(log_dir.glob("inventory_sync_*.log")) if log_dir.exists() else []
    
    if not sync_logs:
        st.info("暂无同步记录")
        st.markdown("""
        **如何运行库存同步:**
        ```bash
        python daily_tasks.py --sync-only
        ```

        **仅调试独立同步脚本时再使用:**
        ```bash
        python src/plugins/inventory_sync/daily_sync.py
        ```
        
        **设置定时任务 (Windows, 推荐主流程入口):**
        ```bash
        schtasks /create /tn "DajianInventorySync" /tr "python daily_tasks.py --sync-only" /sc daily /st 08:00
        ```
        """)
        return
    
    # 显示最近的同步日志
    sync_logs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    
    for log_file in sync_logs[:5]:
        with st.expander(f"📄 {log_file.name}"):
            try:
                content = log_file.read_text(encoding='utf-8')
                st.code(content, language="text")
            except Exception as e:
                st.error(f"读取日志失败: {e}")


def render_sync_settings():
    """同步设置"""
    st.subheader("⚙️ 库存同步设置")
    
    st.markdown("### 大建云仓 API 配置")
    
    # 检查环境变量
    api_key = os.getenv("DAJIAN_API_KEY", "")
    api_secret = os.getenv("DAJIAN_API_SECRET", "")
    
    if api_key and api_secret:
        st.success("✅ API 凭证已配置")
        st.code(f"API Key: {api_key[:8]}...{api_key[-4:]}")
    else:
        st.error("❌ 未配置大建云仓 API")
        st.markdown("""
        请在 `.env` 文件中添加:
        ```
        DAJIAN_API_KEY=your_api_key
        DAJIAN_API_SECRET=your_api_secret
        ```
        """)
    
    st.divider()
    
    st.markdown("### 同步规则")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.checkbox("缺货自动设为0库存", value=True, disabled=True)
        st.checkbox("价格变动自动调整", value=True, disabled=True)
    
    with col2:
        st.number_input("价格变动阈值 (%)", value=5, min_value=1, max_value=20, disabled=True)
        st.number_input("同步间隔 (小时)", value=24, min_value=1, max_value=72, disabled=True)
    
    st.info("💡 设置保存功能开发中，目前使用默认配置")


def run_inventory_sync(limit: int = 0, skip_synced_today: bool = True):
    """运行库存同步
    
    Args:
        limit: 限制同步数量，0=全部
        skip_synced_today: 跳过今日已同步的SKU
    """
    try:
        import os
        from src.clients.dajian_client import DaJianClient
        
        client_id = os.getenv("DAJIAN_API_KEY")
        client_secret = os.getenv("DAJIAN_API_SECRET")
        
        if not client_id or not client_secret:
            st.error("❌ 未配置大建API凭证，请在 .env 中设置")
            return None
        
        dajian = DaJianClient(client_id, client_secret)
        
        # 测试连接
        st.write("✅ 测试大建 API 连接...")
        if not dajian.test_connection():
            st.warning("⚠️ 大建API连接失败，请检查代理设置或API凭证")
            return {'checked': 0, 'out_of_stock': [], 'price_changed': [], 'error': '大建API连接失败'}
        
        # 正常同步
        from src.plugins.inventory_sync.sync_service import InventorySyncService
        
        service = InventorySyncService()
        
        # 获取今日已同步数量
        skipped_synced_today = 0
        if skip_synced_today:
            already_synced = service.get_skus_synced_today()
            skipped_synced_today = len(already_synced)
            if skipped_synced_today > 0:
                st.write(f"⏭️ 跳过今日已同步的 {skipped_synced_today} 个产品")
        
        published = service.get_published_products()
        if not published:
            st.warning("暂无已刊登产品可同步 (状态= PUBLISHED)")
            return {'checked': 0, 'out_of_stock': [], 'price_changed': []}
        
        st.write(f"📦 发现 {len(published)} 个已刊登产品，开始同步...")
        
        # 使用快速模式同步（跳过eBay状态检查）
        results = service.sync_all(skip_ebay_check=True, limit=limit, skip_synced_today=skip_synced_today)
        
        # 处理结果
        if isinstance(results, list):
            out_of_stock = [r.sku for r in results if r.action == 'out_of_stock']
            price_changed = [r.sku for r in results if r.action == 'price_updated']
            no_change = sum(1 for r in results if r.action == 'no_change')
            skipped = sum(1 for r in results if r.action == 'skipped')
            errors = sum(1 for r in results if r.action == 'error')
            
            return {
                'checked': len(results),
                'out_of_stock': out_of_stock,
                'price_changed': price_changed,
                'no_change': no_change,
                'skipped': skipped,
                'errors': errors,
                'skipped_synced_today': skipped_synced_today
            }
        else:
            return {
                'checked': results.get('total_checked', 0),
                'out_of_stock': results.get('out_of_stock_skus', []),
                'price_changed': results.get('price_changed_skus', []),
                'skipped_synced_today': skipped_synced_today
            }
            
    except Exception as e:
        import traceback
        st.error(f"同步失败: {e}")
        st.code(traceback.format_exc())
        return None


# 页面入口
if __name__ == "__main__":
    st.set_page_config(page_title="库存管理", page_icon="📦", layout="wide")
    render_inventory_management()
