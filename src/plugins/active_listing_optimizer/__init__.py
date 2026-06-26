"""
eBay Active Listings Title Optimizer Plugin

集成外部项目: C:/Users/poonx/eBay_Title_Optimization
功能: 自动优化 eBay 在线商品的标题
"""
import streamlit as st
import os
import sys
from pathlib import Path
from datetime import datetime
from src.plugins import PluginBase, register_plugin, SharedServices

# 添加外部项目路径 (使用正斜杠避免转义问题)
EXTERNAL_PROJECT_PATH = r"C:\Users\poonx\eBay_Title_Optimization"
EBAY_PROJECT_PATH = r"C:\Users\poonx\Ebay"

sys.path.insert(0, EXTERNAL_PROJECT_PATH)
sys.path.insert(0, EBAY_PROJECT_PATH)


@register_plugin
class ActiveListingOptimizerPlugin(PluginBase):
    """eBay 在售商品标题优化插件"""
    
    name = "在售标题优化"
    description = "优化 eBay 在线商品的标题以提升搜索排名"
    version = "1.0.0"
    icon = "📈"
    requires_own_db = True
    
    def __init__(self):
        self.db_path = None
        self._optimizer = None
        self._trading_client = None
        self._market_research = None
    
    def on_load(self, shared_services: SharedServices):
        """插件加载时初始化"""
        plugin_dir = Path(__file__).parent
        self.db_path = str(plugin_dir / "optimization_history.db")
        shared_services.plugin_db_paths[self.name] = self.db_path
        self._init_db()
    
    def _init_db(self):
        """初始化历史记录数据库"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS optimization_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL,
                original_title TEXT NOT NULL,
                optimized_title TEXT,
                char_count INTEGER,
                status TEXT DEFAULT 'pending',
                updated_to_ebay INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 创建分页追踪表（用于轮换优化）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pagination_state (
                id INTEGER PRIMARY KEY,
                last_page INTEGER DEFAULT 1,
                last_item_index INTEGER DEFAULT 0,
                total_items INTEGER DEFAULT 0,
                current_round_id INTEGER DEFAULT 1,
                last_run_date TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 添加 round_id 列到 optimization_history（如果不存在）
        try:
            cursor.execute("ALTER TABLE optimization_history ADD COLUMN round_id INTEGER DEFAULT 0")
        except:
            pass  # 列已存在
        
        # 添加 current_round_id 列到 pagination_state（兼容旧数据库）
        try:
            cursor.execute("ALTER TABLE pagination_state ADD COLUMN current_round_id INTEGER DEFAULT 1")
        except:
            pass  # 列已存在
        
        # 初始化分页状态记录
        cursor.execute("INSERT OR IGNORE INTO pagination_state (id, last_page, current_round_id) VALUES (1, 1, 1)")
        
        conn.commit()
        conn.close()
    
    def _get_optimizer(self):
        """懒加载 Qwen 优化器"""
        if self._optimizer is None:
            try:
                from qwen_optimizer import QwenOptimizer
                api_key = os.getenv("QWEN_API_KEY")
                if api_key:
                    self._optimizer = QwenOptimizer(api_key=api_key)
            except Exception as e:
                st.error(f"加载优化器失败: {e}")
        return self._optimizer
    
    def _get_trading_client(self, oauth):
        """获取 eBay Trading Client"""
        if self._trading_client is None:
            try:
                from src.clients.ebay_client import EbayClient
                from src.clients.ebay_trading_client import EbayTradingClient
                
                ebay = EbayClient(
                    os.getenv("EBAY_APP_ID"),
                    os.getenv("EBAY_CERT_ID"),
                    os.getenv("EBAY_DEV_ID"),
                    env="production"
                )
                self._trading_client = EbayTradingClient(ebay)
            except Exception as e:
                st.error(f"加载 Trading Client 失败: {e}")
        return self._trading_client
    
    def render(self, shared_services: SharedServices):
        """渲染插件UI"""
        st.header("📈 eBay 在售商品标题优化")
        st.markdown("使用 AI 优化你的在售商品标题，提高搜索排名和销量。")
        
        # 检查授权状态
        oauth = shared_services.ebay_oauth
        if not oauth or not oauth.is_authorized():
            st.error("❌ eBay 未授权 - 请先在 🔐 eBay 授权 页面完成授权")
            return
        
        st.success("✅ eBay 已授权")
        
        # 创建标签页
        tab1, tab2, tab3, tab4 = st.tabs(["🎯 单个优化", "📦 批量优化", "📜 优化历史", "⏰ 定时任务"])
        
        with tab1:
            self._render_single_optimize(oauth)
        
        with tab2:
            self._render_batch_optimize(oauth)
        
        with tab3:
            self._render_history()
        
        with tab4:
            self._render_scheduled_task()
    
    def _render_single_optimize(self, oauth):
        """单个标题优化 - 通过 Item ID 获取、优化并更新"""
        st.subheader("🎯 单个标题优化")
        
        st.info("输入 eBay Item ID，自动获取标题、AI 优化、一键更新到 eBay")
        
        col1, col2 = st.columns(2)
        
        with col1:
            item_id = st.text_input(
                "eBay Item ID",
                placeholder="如: 366126039400",
                key="single_item_id"
            )
            
            fetch_btn = st.button("📥 获取标题", key="fetch_single")
            
            # 如果已获取标题，显示它
            if 'single_item_data' in st.session_state:
                item_data = st.session_state['single_item_data']
                st.text_area(
                    "原始标题",
                    value=item_data.get('Title', ''),
                    height=80,
                    disabled=True,
                    key="single_orig_title"
                )
        
        with col2:
            st.markdown("**优化结果**")
            
            # 获取标题
            if fetch_btn and item_id:
                with st.spinner("正在从 eBay 获取..."):
                    item_data = self._fetch_single_item(oauth, item_id)
                    if item_data:
                        st.session_state['single_item_data'] = item_data
                        st.success(f"✅ 获取成功: {item_data.get('Title', '')[:50]}...")
                        st.rerun()
                    else:
                        st.error("❌ 获取失败，请检查 Item ID")
            
            # 显示优化结果
            if 'single_item_data' in st.session_state:
                item_data = st.session_state['single_item_data']
                
                if 'optimized_title' not in st.session_state:
                    if st.button("🚀 AI 优化标题", type="primary", key="optimize_single"):
                        optimizer = self._get_optimizer()
                        if optimizer:
                            with st.spinner("AI 正在优化..."):
                                try:
                                    optimized = optimizer.optimize_title(
                                        item_data.get('Title', ''),
                                        category=item_data.get('CategoryID', ''),
                                        max_length=80
                                    )
                                    st.session_state['optimized_title'] = optimized
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"优化失败: {e}")
                        else:
                            st.error("优化器未初始化")
                else:
                    optimized = st.session_state['optimized_title']
                    st.text_area("优化后标题", value=optimized, height=80, key="opt_result")
                    
                    # 字符数对比
                    orig_len = len(item_data.get('Title', ''))
                    opt_len = len(optimized)
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.metric("原始长度", f"{orig_len} 字符")
                    with col_b:
                        delta = opt_len - orig_len
                        st.metric("优化后", f"{opt_len} 字符", delta=f"{delta:+d}")
                    
                    # 更新到 eBay 按钮
                    col_u1, col_u2 = st.columns(2)
                    with col_u1:
                        if st.button("✅ 更新到 eBay", type="primary", key="update_single"):
                            with st.spinner("正在更新到 eBay..."):
                                success = self._update_item_title(oauth, item_data['ItemID'], optimized)
                                if success:
                                    self._save_history(item_data['ItemID'], item_data['Title'], optimized, updated_to_ebay=1)
                                    st.success("🎉 已更新到 eBay!")
                                    # 清理状态
                                    del st.session_state['single_item_data']
                                    del st.session_state['optimized_title']
                                else:
                                    st.error("更新失败")
                    with col_u2:
                        if st.button("🔄 重新优化", key="retry_single"):
                            del st.session_state['optimized_title']
                            st.rerun()
            else:
                st.info("输入 Item ID 后点击获取标题")
    
    def _render_batch_optimize(self, oauth):
        """批量优化在售商品"""
        st.subheader("📦 批量优化在售商品")
        
        st.warning("⚠️ 批量优化会直接更新你的 eBay 商品标题，请谨慎操作！")
        
        # 显示分页状态
        pagination_info = self._get_pagination_state()
        current_round = self._get_current_round_id()
        
        if pagination_info:
            col_info1, col_info2, col_info3, col_info4 = st.columns(4)
            with col_info1:
                st.info(f"📄 当前页码: {pagination_info['last_page']}")
            with col_info2:
                st.info(f"🔄 当前轮回: 第 {current_round} 轮")
            with col_info3:
                st.info(f"📊 本轮已优化: {pagination_info['last_item_index']}")
            with col_info4:
                st.info(f"📅 上次运行: {pagination_info.get('last_run_date', '无')}")
        
        # 配置
        col1, col2, col3 = st.columns(3)
        with col1:
            batch_size = st.number_input("优化数量", min_value=1, max_value=100, value=10, key="batch_size")
        with col2:
            auto_update = st.checkbox("自动更新到 eBay", value=False, key="auto_update")
        with col3:
            skip_optimized = st.checkbox("跳过本轮已优化", value=True, key="skip_optimized", 
                                         help="跳过当前轮回已优化的商品，保持店铺活跃")
        
        # 清除之前的获取结果（如果数量变了）
        if 'last_batch_size' not in st.session_state:
            st.session_state['last_batch_size'] = batch_size
        
        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("📥 获取下一批商品", key="fetch_listings"):
                # 清除旧数据
                if 'active_listings' in st.session_state:
                    del st.session_state['active_listings']
                st.session_state['last_batch_size'] = batch_size
                
                with st.spinner(f"正在从 eBay 获取 {batch_size} 个未优化商品..."):
                    listings = self._fetch_active_listings_with_rotation(oauth, limit=batch_size, skip_optimized=skip_optimized)
                    if listings:
                        st.session_state['active_listings'] = listings
                        st.success(f"✅ 获取到 {len(listings)} 个商品")
                    else:
                        st.warning("没有找到更多待优化商品，可能已全部优化完成，点击「重置分页」从头开始")
        
        with col_btn2:
            if st.button("🔄 开始新轮回", key="reset_pagination"):
                self._start_new_round()
                st.success("✅ 已开始新一轮优化，下次将从第一页开始获取")
                st.rerun()
        
        # 显示获取的商品
        if 'active_listings' in st.session_state and st.session_state['active_listings']:
            listings = st.session_state['active_listings']
            
            st.markdown(f"**待优化商品 ({len(listings)} 个)**")
            
            # 显示表格
            import pandas as pd
            df = pd.DataFrame(listings)
            st.dataframe(df[['ItemID', 'Title']] if 'ItemID' in df.columns else df, use_container_width=True)
            
            if st.button("🚀 开始批量优化", type="primary", key="start_batch"):
                self._run_batch_optimization(listings, auto_update, oauth)
    
    def _fetch_active_listings(self, oauth, limit=10):
        """从 eBay 获取在售商品 (严格限制数量)"""
        try:
            # 尝试使用 Trading API
            trading = self._get_trading_client(oauth)
            if trading:
                import xml.etree.ElementTree as ET
                
                xml_payload = f"""
                <ActiveList>
                    <Include>true</Include>
                    <Pagination>
                        <EntriesPerPage>{limit}</EntriesPerPage>
                        <PageNumber>1</PageNumber>
                    </Pagination>
                </ActiveList>
                <DetailLevel>ReturnAll</DetailLevel>
                """
                
                response = trading.call("GetMyeBaySelling", xml_payload)
                root = ET.fromstring(response)
                
                ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
                items = root.findall('.//ebay:Item', ns)
                
                listings = []
                for item in items:
                    # 严格限制数量
                    if len(listings) >= limit:
                        break
                        
                    item_id = item.find('ebay:ItemID', ns)
                    title = item.find('ebay:Title', ns)
                    category = item.find('ebay:PrimaryCategory/ebay:CategoryID', ns)
                    
                    if item_id is not None and title is not None:
                        listings.append({
                            "ItemID": item_id.text,
                            "Title": title.text,
                            "CategoryID": category.text if category is not None else ""
                        })
                
                # 再次确保不超过限制
                return listings[:limit]
        except Exception as e:
            st.error(f"获取商品失败: {e}")
            return []
    
    def _get_pagination_state(self) -> dict:
        """获取分页状态"""
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT last_page, last_item_index, total_items, last_run_date FROM pagination_state WHERE id = 1")
            row = cursor.fetchone()
            conn.close()
            if row:
                return {
                    "last_page": row[0] or 1,
                    "last_item_index": row[1] or 0,
                    "total_items": row[2] or 0,
                    "last_run_date": row[3]
                }
        except:
            pass
        return {"last_page": 1, "last_item_index": 0, "total_items": 0, "last_run_date": None}
    
    def _update_pagination_state(self, page: int, item_index: int, total_items: int = 0):
        """更新分页状态"""
        import sqlite3
        from datetime import datetime
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE pagination_state 
                SET last_page = ?, last_item_index = ?, total_items = ?, 
                    last_run_date = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE id = 1
            """, (page, item_index, total_items, datetime.now().strftime('%Y-%m-%d %H:%M')))
            conn.commit()
            conn.close()
        except Exception as e:
            pass  # 静默处理
    
    def _reset_pagination_state(self):
        """重置分页状态，从头开始"""
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE pagination_state SET last_page = 1, last_item_index = 0 WHERE id = 1")
            conn.commit()
            conn.close()
        except:
            pass
    
    def _start_new_round(self):
        """开始新一轮优化（轮回 +1，重置分页）"""
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            # 轮回 ID +1，分页重置
            cursor.execute("""
                UPDATE pagination_state 
                SET current_round_id = current_round_id + 1, 
                    last_page = 1, 
                    last_item_index = 0 
                WHERE id = 1
            """)
            conn.commit()
            conn.close()
        except:
            pass
    
    def _get_current_round_optimized_item_ids(self) -> set:
        """获取当前轮回已优化的 Item ID（只跳过本轮，不跳过之前轮回）"""
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 获取当前轮回 ID
            cursor.execute("SELECT current_round_id FROM pagination_state WHERE id = 1")
            row = cursor.fetchone()
            current_round = row[0] if row else 1
            
            # 只获取当前轮回已优化的商品
            cursor.execute(
                "SELECT DISTINCT item_id FROM optimization_history WHERE round_id = ?",
                (current_round,)
            )
            rows = cursor.fetchall()
            conn.close()
            return {row[0] for row in rows}
        except:
            return set()
    
    def _get_current_round_id(self) -> int:
        """获取当前轮回 ID"""
        import sqlite3
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT current_round_id FROM pagination_state WHERE id = 1")
            row = cursor.fetchone()
            conn.close()
            return row[0] if row else 1
        except:
            return 1
    
    def _fetch_active_listings_with_rotation(self, oauth, limit: int = 10, skip_optimized: bool = True):
        """从 eBay 获取在售商品，支持分页轮换，只跳过当前轮回已优化商品"""
        try:
            trading = self._get_trading_client(oauth)
            if not trading:
                return []
            
            import xml.etree.ElementTree as ET
            
            # 获取当前分页状态
            pagination_state = self._get_pagination_state()
            current_page = pagination_state['last_page']
            
            # 获取当前轮回已优化的商品 ID（只跳过本轮，不跳过之前轮回）
            optimized_ids = self._get_current_round_optimized_item_ids() if skip_optimized else set()
            
            # 每页获取更多商品，以便过滤后仍有足够数量
            entries_per_page = min(100, limit * 3)  # 获取3倍数量，过滤后取 limit 个
            
            all_listings = []
            max_pages = 10  # 最多遍历10页
            
            for page_offset in range(max_pages):
                page_num = current_page + page_offset
                
                xml_payload = f"""
                <ActiveList>
                    <Include>true</Include>
                    <Pagination>
                        <EntriesPerPage>{entries_per_page}</EntriesPerPage>
                        <PageNumber>{page_num}</PageNumber>
                    </Pagination>
                </ActiveList>
                <DetailLevel>ReturnAll</DetailLevel>
                """
                
                response = trading.call("GetMyeBaySelling", xml_payload)
                root = ET.fromstring(response)
                
                ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
                items = root.findall('.//ebay:Item', ns)
                
                if not items:
                    # 没有更多商品了，从第一页重新开始
                    if page_offset == 0 and current_page > 1:
                        # 重置分页，从头开始
                        self._reset_pagination_state()
                        current_page = 1
                        continue
                    else:
                        break
                
                for item in items:
                    item_id_elem = item.find('ebay:ItemID', ns)
                    title_elem = item.find('ebay:Title', ns)
                    category_elem = item.find('ebay:PrimaryCategory/ebay:CategoryID', ns)
                    
                    if item_id_elem is not None and title_elem is not None:
                        item_id = item_id_elem.text
                        
                        # 跳过已优化的商品
                        if skip_optimized and item_id in optimized_ids:
                            continue
                        
                        all_listings.append({
                            "ItemID": item_id,
                            "Title": title_elem.text,
                            "CategoryID": category_elem.text if category_elem is not None else ""
                        })
                        
                        # 如果已经收集够了，停止
                        if len(all_listings) >= limit:
                            # 更新分页状态：记录当前页和已处理的索引
                            total_count_elem = root.find('.//ebay:PaginationResult/ebay:TotalNumberOfEntries', ns)
                            total_count = int(total_count_elem.text) if total_count_elem is not None else 0
                            
                            self._update_pagination_state(
                                page=page_num + 1,  # 下次从下一页开始
                                item_index=pagination_state['last_item_index'] + len(all_listings),
                                total_items=total_count
                            )
                            return all_listings[:limit]
                
                # 当前页处理完了，继续下一页
            
            # 如果遍历完还没有足够的商品，返回收集到的
            if all_listings:
                self._update_pagination_state(
                    page=1,  # 已经全部处理完，下次从头开始
                    item_index=0,
                    total_items=0
                )
            
            return all_listings[:limit] if all_listings else []
            
        except Exception as e:
            st.error(f"获取商品失败: {e}")
            import traceback
            st.error(traceback.format_exc())
            return []
    
    def _fetch_single_item(self, oauth, item_id: str) -> dict:
        """从 eBay 获取单个商品信息 (包括描述)"""
        try:
            trading = self._get_trading_client(oauth)
            if trading:
                import xml.etree.ElementTree as ET
                import html
                
                xml_payload = f"""
                <ItemID>{item_id}</ItemID>
                <DetailLevel>ReturnAll</DetailLevel>
                <IncludeItemSpecifics>true</IncludeItemSpecifics>
                """
                
                response = trading.call("GetItem", xml_payload)
                root = ET.fromstring(response)
                
                ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
                
                item = root.find('.//ebay:Item', ns)
                if item is not None:
                    title_elem = item.find('ebay:Title', ns)
                    category_elem = item.find('ebay:PrimaryCategory/ebay:CategoryID', ns)
                    desc_elem = item.find('ebay:Description', ns)
                    sku_elem = item.find('ebay:SKU', ns)
                    
                    # 清理 HTML 描述，提取纯文本
                    description = ""
                    if desc_elem is not None and desc_elem.text:
                        # 移除 HTML 标签
                        import re
                        desc_html = desc_elem.text
                        desc_text = re.sub(r'<[^>]+>', ' ', desc_html)
                        desc_text = html.unescape(desc_text)
                        desc_text = re.sub(r'\s+', ' ', desc_text).strip()
                        # 保留完整描述（最多5000字符，避免过长）
                        description = desc_text[:5000]
                    
                    return {
                        "ItemID": item_id,
                        "SKU": sku_elem.text if sku_elem is not None else "",
                        "Title": title_elem.text if title_elem is not None else "",
                        "CategoryID": category_elem.text if category_elem is not None else "",
                        "Description": description
                    }
        except Exception as e:
            pass  # 静默失败，不显示错误
        return None
    
    def _update_item_title(self, oauth, item_id: str, new_title: str, sku: str = "") -> tuple:
        """
        更新 eBay 商品标题 - End-to-End 完整版
        
        功能：
        1. 优先使用 Trading API (ReviseFixedPriceItem)
        2. 自动捕获 21919474 错误并降级到 Inventory API (REST)
        3. 全链路日志记录，便于 UI 调试
        
        返回: (success: bool, skip_reason: str or None, debug_log: str)
        """
        exec_log = [f"Start _update_item_title for {item_id}, SKU='{sku}'"]
        last_error = ""
        response_pretty = "No Response"
        
        try:
            trading = self._get_trading_client(oauth)
            if not trading:
                msg = "Trading API Init Failed"
                exec_log.append(msg)
                return False, msg, "\n".join(exec_log)
            
            import xml.etree.ElementTree as ET
            import html
            
            safe_title = html.escape(new_title)
            
            # 使用带 Namespace 的 Payload
            xml_payload = f"""
            <Item xmlns="urn:ebay:apis:eBLBaseComponents">
                <ItemID>{item_id}</ItemID>
                <Title>{safe_title}</Title>
            </Item>
            """
            
            # 优先尝试 ReviseFixedPriceItem
            api_calls = ["ReviseFixedPriceItem", "ReviseItem"]
            
            for call_name in api_calls:
                try:
                    exec_log.append(f"Calling {call_name}...")
                    response = trading.call(call_name, xml_payload)
                    response_pretty = response
                    
                    root = ET.fromstring(response)
                    ns = {'ebay': 'urn:ebay:apis:eBLBaseComponents'}
                    
                    ack = root.find('.//ebay:Ack', ns)
                    if ack is not None and ack.text in ['Success', 'Warning']:
                        exec_log.append(f"{call_name} Success!")
                        return True, None, "\n".join(exec_log) + "\n\nResponse:\n" + response
                    
                    # 处理错误
                    errors = root.findall('.//ebay:Errors', ns)
                    for error in errors:
                        code = error.find('ebay:ErrorCode', ns)
                        short_msg = error.find('ebay:ShortMessage', ns)
                        long_msg = error.find('ebay:LongMessage', ns)
                        
                        code_txt = code.text if code is not None else "?"
                        msg_txt = long_msg.text if long_msg is not None else (short_msg.text if short_msg is not None else "Unknown")
                        
                        log_line = f"Error [{code_txt}]: {msg_txt}"
                        print(f"   [ERROR] {log_line}")
                        exec_log.append(log_line)
                        last_error = log_line
                        
                        # 291: Item Ended
                        if code_txt == '291':
                            return False, "ended", "\n".join(exec_log)
                            
                        # 21919474: Inventory Item -> Try REST API
                        if code_txt == '21919474':
                            # 如果 SKU 为空
                            if not sku:
                                exec_log.append("WARNING: 21919474 detected but SKU is empty!")
                                continue
                                
                            msg = f"Detection Inventory Item (SKU: {sku}), attempting REST API..."
                            print(f"   [INFO] {msg}")
                            exec_log.append(msg)
                            
                            try:
                                # Get Token (Robust Strategy)
                                token = None
                                
                                # 1. Try from OAuth object (Most reliable for Streamlit apps)
                                try:
                                    if hasattr(oauth, 'token') and isinstance(oauth.token, dict):
                                        token = oauth.token.get('access_token')
                                        if token: exec_log.append("Got token from oauth.token")
                                except: pass
                                
                                # 2. Try from Trading Client wrapper
                                if not token:
                                    try:
                                        if hasattr(trading, 'ebay_client') and hasattr(trading.ebay_client, 'get_oauth_token'):
                                            token = trading.ebay_client.get_oauth_token()
                                            if token: exec_log.append("Got token from trading.ebay_client")
                                    except: pass
                                
                                # 3. Try from raw headers (if it were a raw connection)
                                if not token:
                                    try:
                                        if hasattr(trading, 'headers'):
                                            token = trading.headers.get('X-EBAY-API-IAF-TOKEN')
                                            if token: exec_log.append("Got token from trading.headers")
                                    except: pass
                                
                                if not token:
                                    exec_log.append("ERROR: No Token available for REST API")
                                    continue
                                    
                                import requests
                                import urllib.parse
                                
                                safe_sku = urllib.parse.quote(sku)
                                url = f"https://api.ebay.com/sell/inventory/v1/inventory_item/{safe_sku}"
                                headers = {
                                    'Authorization': f'Bearer {token}',
                                    'Content-Type': 'application/json',
                                    'Content-Language': 'en-US'
                                }
                                
                                # GET -> MODIFY -> PUT
                                exec_log.append(f"REST GET {url}")
                                get_resp = requests.get(url, headers=headers)
                                if get_resp.status_code == 200:
                                    inv_item = get_resp.json()
                                    if 'product' not in inv_item: inv_item['product'] = {}
                                    inv_item['product']['title'] = new_title
                                    
                                    exec_log.append("REST PUT...")
                                    put_resp = requests.put(url, headers=headers, json=inv_item)
                                    if put_resp.status_code in [200, 204]:
                                        exec_log.append("REST API Success!")
                                        return True, None, "\n".join(exec_log) + "\nFixed via Inventory API!"
                                    else:
                                        exec_log.append(f"REST PUT Error: {put_resp.status_code} {put_resp.text}")
                                else:
                                    exec_log.append(f"REST GET Error: {get_resp.status_code} {get_resp.text}")
                                    
                            except Exception as rest_e:
                                exec_log.append(f"REST API Exception: {rest_e}")

                except Exception as e:
                    last_error = f"Exception: {e}"
                    exec_log.append(last_error)
            
            # All attempts failed
            return False, last_error if last_error else "Update Failed", "\n".join(exec_log) + "\n\nLast Response:\n" + response_pretty

        except Exception as e:
            return False, str(e)[:50], f"Critical Exception: {e}"



    def _run_batch_optimization(self, listings, auto_update, oauth):
        """执行批量优化 - 结合产品描述生成更好的标题，支持暂停"""
        optimizer = self._get_optimizer()
        if not optimizer:
            st.error("优化器未初始化")
            return
        
        # ===== 初始化暂停状态 =====
        if 'batch_stop_requested' not in st.session_state:
            st.session_state['batch_stop_requested'] = False
        
        # 暂停按钮
        stop_col, status_col = st.columns([1, 4])
        with stop_col:
            if st.button("⏹️ 停止优化", type="secondary", key="stop_batch_btn"):
                st.session_state['batch_stop_requested'] = True
                st.warning("正在停止...将在当前商品完成后停止")
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        results_container = st.container()
        
        results = []
        total = len(listings)
        skipped_ended = 0
        
        for idx, item in enumerate(listings):
            # ===== 检查暂停请求 =====
            if st.session_state.get('batch_stop_requested', False):
                st.warning(f"⏹️ 已停止！完成 {idx}/{total} 个商品")
                st.session_state['batch_stop_requested'] = False  # 重置
                break
            
            progress = (idx + 1) / total
            progress_bar.progress(progress)
            item_id = item.get('ItemID', '')
            status_text.text(f"正在优化 {idx + 1}/{total}: {item_id}")
            
            try:
                original = item.get('Title', '')
                
                # 获取产品描述和 SKU
                description = ""
                sku = ""
                try:
                    item_detail = self._fetch_single_item(oauth, item_id)
                    if item_detail:
                        description = item_detail.get('Description', '')
                        sku = item_detail.get('SKU', '')
                except:
                    pass
                
                optimized = optimizer.optimize_title(
                    original,
                    category=item.get('CategoryID', ''),
                    max_length=80,
                    description=description
                )
                
                updated_to_ebay = 0
                update_status = ""
                
                # 更新到 eBay
                debug_log = ""
                if auto_update:
                    success, skip_reason, debug_log = self._update_item_title(oauth, item_id, optimized, sku=sku)
                    if success:
                        updated_to_ebay = 1
                        update_status = " ✅已更新"
                    elif skip_reason == "ended":
                        skipped_ended += 1
                        update_status = " ⏭️已结束"
                    else:
                        update_status = f" ❌{skip_reason[:20]}"
                
                results.append({
                    "ItemID": item_id,
                    "Original": original,  # 完整显示
                    "Optimized": optimized,  # 完整显示
                    "Chars": len(optimized),
                    "Status": f"✅{update_status}",
                    "Debug": debug_log  # 用于在 UI 显示
                })
                
                # 保存历史 (跳过已结束的)
                if skip_reason != "ended":
                    self._save_history(item_id, original, optimized, updated_to_ebay)
                    
            except Exception as e:
                results.append({
                    "ItemID": item_id,
                    "Original": item.get('Title', ''),  # 完整显示
                    "Optimized": "",
                    "Chars": 0,
                    "Status": f"❌ {str(e)[:25]}",
                    "Debug": str(e)
                })
        
        progress_bar.progress(1.0)
        status_text.text("优化完成！")
        
        # 清除 session state
        if 'active_listings' in st.session_state:
            del st.session_state['active_listings']
        st.session_state['batch_stop_requested'] = False
        
        # 显示结果
        with results_container:
            import pandas as pd
            df = pd.DataFrame(results)
            
            # 显示完整表格，允许横向滚动
            # 隐藏 Debug 列，太大且不仅用户看
            display_df = df.drop(columns=['Debug'], errors='ignore')
            st.dataframe(display_df, use_container_width=True, height=400)
            
            # 增加一个区域显示选定行的日志（这里简化为显示所有错误日志）
            with st.expander("🔍 查看详细 API 日志 (Debugging)"):
                for r in results:
                    if "❌" in r['Status'] and r.get('Debug'):
                        st.markdown(f"**ItemID: {r['ItemID']}**")
                        st.code(r['Status'], language="text")
                        st.text("eBay API Response:")
                        st.code(r['Debug'], language="xml")
                        st.divider()
            
            success_count = len([r for r in results if "✅" in r.get('Status', '') and "已更新" in r.get('Status', '')])
            
            # 统计信息
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("成功更新", f"{success_count} 个")
            with col2:
                st.metric("跳过已结束", f"{skipped_ended} 个")
            with col3:
                fail_count = len(results) - success_count - skipped_ended
                st.metric("失败", f"{fail_count} 个")
    
    def _save_history(self, item_id: str, original: str, optimized: str, updated_to_ebay: int = 0):
        """保存优化历史（包含当前轮回ID）"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 获取当前轮回 ID
        current_round = self._get_current_round_id()
        
        cursor.execute(
            """INSERT INTO optimization_history 
               (item_id, original_title, optimized_title, char_count, status, updated_to_ebay, round_id) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_id, original, optimized, len(optimized), 'completed', updated_to_ebay, current_round)
        )
        conn.commit()
        conn.close()
    
    def _render_history(self):
        """渲染优化历史"""
        st.subheader("📜 优化历史")
        
        st.info("""
        **优化历史说明：**
        - 记录所有通过本插件优化过的标题
        - 显示最近 50 条记录（按时间倒序）
        - 可以对比原始标题和优化后标题的效果
        - `updated_to_ebay` 列显示是否已同步到 eBay
        """)
        
        import sqlite3
        import pandas as pd
        
        if not self.db_path or not Path(self.db_path).exists():
            st.info("暂无优化历史")
            return
        
        conn = sqlite3.connect(self.db_path)
        
        # 获取总数
        total_count = pd.read_sql_query("SELECT COUNT(*) as cnt FROM optimization_history", conn)['cnt'].iloc[0]
        
        df = pd.read_sql_query(
            """SELECT item_id, original_title, optimized_title, char_count, updated_to_ebay, created_at 
               FROM optimization_history 
               ORDER BY id DESC LIMIT 50""",
            conn
        )
        conn.close()
        
        if df.empty:
            st.info("暂无优化历史")
        else:
            # 统计
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("总优化数", total_count)
            with col2:
                avg_chars = df['char_count'].mean()
                st.metric("平均字符数", f"{avg_chars:.1f}")
            with col3:
                today_count = len(df[df['created_at'].str.startswith(datetime.now().strftime('%Y-%m-%d'))])
                st.metric("今日优化", today_count)
            with col4:
                updated_count = df['updated_to_ebay'].sum()
                st.metric("已同步到eBay", int(updated_count))
            
            st.divider()
            st.markdown(f"**最近 50 条记录** (总共 {total_count} 条)")
            st.dataframe(df, use_container_width=True)
    
    def _render_scheduled_task(self):
        """渲染定时任务配置页面"""
        st.subheader("⏰ 每日定时优化任务")
        
        st.markdown("""
        ### 定时任务说明
        
        本功能使用 **Windows 任务计划程序** 在每天固定时间自动优化店铺标题。
        
        **工作原理：**
        1. 每天凌晨自动运行优化脚本
        2. 从 eBay 获取所有在售商品
        3. 使用 AI 优化标题（结合产品描述）
        4. 自动更新到 eBay
        5. 记录优化历史
        """)
        
        # 检查定时任务状态
        import subprocess
        task_name = "eBay_Daily_Title_Optimization"
        
        try:
            result = subprocess.run(
                ['schtasks', '/Query', '/TN', task_name, '/FO', 'LIST'],
                capture_output=True, text=True, timeout=10
            )
            
            if result.returncode == 0:
                st.success(f"✅ 定时任务已配置: {task_name}")
                
                # 解析任务信息
                output = result.stdout
                with st.expander("📋 任务详情", expanded=True):
                    st.code(output)
                
                # 提供禁用/删除按钮
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("⏸️ 禁用任务", key="disable_task"):
                        disable_result = subprocess.run(
                            ['schtasks', '/Change', '/TN', task_name, '/Disable'],
                            capture_output=True, text=True
                        )
                        if disable_result.returncode == 0:
                            st.success("✅ 任务已禁用")
                            st.rerun()
                        else:
                            st.error(f"禁用失败: {disable_result.stderr}")
                with col2:
                    if st.button("🗑️ 删除任务", key="delete_task"):
                        del_result = subprocess.run(
                            ['schtasks', '/Delete', '/TN', task_name, '/F'],
                            capture_output=True, text=True
                        )
                        if del_result.returncode == 0:
                            st.success("✅ 任务已删除")
                            st.rerun()
                        else:
                            st.error(f"删除失败: {del_result.stderr}")
            else:
                st.warning("⚠️ 定时任务未配置")
                
                st.markdown("""
                ### 如何创建定时任务
                
                **方法 1：使用命令行**
                
                在 PowerShell 中运行（以管理员身份）：
                """)
                
                script_path = Path(__file__).parent / "daily_optimize.py"
                python_path = r"C:\Users\poonx\Dajian_Listing_Tool\.venv\Scripts\python.exe"
                
                cmd = f'''schtasks /Create /TN "{task_name}" /TR "\\"{python_path}\\" \\"{script_path}\\"" /SC DAILY /ST 03:00 /F'''
                
                st.code(cmd, language="powershell")
                
                st.markdown("""
                **方法 2：使用任务计划程序 GUI**
                
                1. 按 `Win + R`，输入 `taskschd.msc`
                2. 点击 "创建基本任务"
                3. 设置每天凌晨 3:00 运行
                4. 操作选择 "启动程序"
                5. 程序选择 Python 解释器
                6. 参数填写脚本路径
                """)
                
                # 提供一键创建按钮
                st.divider()
                col1, col2 = st.columns(2)
                with col1:
                    run_time = st.time_input("运行时间", value=datetime.strptime("03:00", "%H:%M").time(), key="task_time")
                with col2:
                    batch_size_task = st.number_input("每次优化数量", min_value=10, max_value=500, value=50, key="task_batch")
                
                if st.button("🚀 创建定时任务", type="primary", key="create_task"):
                    time_str = run_time.strftime("%H:%M")
                    create_cmd = f'schtasks /Create /TN "{task_name}" /TR "\\"{python_path}\\" \\"{script_path}\\" --batch-size {batch_size_task}" /SC DAILY /ST {time_str} /F'
                    
                    create_result = subprocess.run(
                        create_cmd,
                        shell=True,
                        capture_output=True,
                        text=True
                    )
                    
                    if create_result.returncode == 0:
                        st.success(f"✅ 定时任务创建成功！每天 {time_str} 自动优化 {batch_size_task} 个标题")
                        st.rerun()
                    else:
                        st.error(f"创建失败: {create_result.stderr}")
                        st.info("💡 提示：可能需要以管理员身份运行或手动在任务计划程序中创建")
                        
        except Exception as e:
            st.error(f"检查定时任务失败: {e}")

