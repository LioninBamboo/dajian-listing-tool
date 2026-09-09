"""
eBay Title Optimizer Plugin - 示例插件

这是一个示例，展示如何将外部的eBay标题优化项目集成为插件。

集成步骤:
1. 将你的标题优化代码复制到这个文件夹
2. 修改此文件，调用你的优化逻辑
3. 使用 shared_services.ebay_oauth 获取 eBay API 访问权限
"""

import streamlit as st
from pathlib import Path
from src.plugins import PluginBase, register_plugin, SharedServices


@register_plugin
class TitleOptimizerPlugin(PluginBase):
    """eBay 标题优化插件"""
    
    name = "标题优化器"
    description = "AI驱动的eBay标题优化工具"
    version = "1.0.0"
    icon = "✨"
    requires_own_db = True  # 使用独立数据库
    
    def __init__(self):
        self.db_path = None
        self._optimizer = None
    
    def on_load(self, shared_services: SharedServices):
        """插件加载时初始化"""
        # 设置插件专用数据库
        plugin_dir = Path(__file__).parent
        self.db_path = str(plugin_dir / "title_optimizer.db")
        shared_services.plugin_db_paths[self.name] = self.db_path
        
        # 初始化数据库
        self._init_db()
    
    def _init_db(self):
        """初始化插件专用数据库"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS optimization_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_title TEXT NOT NULL,
                optimized_title TEXT,
                score REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
    
    def render(self, shared_services: SharedServices):
        """渲染插件UI"""
        st.header("✨ eBay 标题优化器")
        st.markdown("使用 AI 优化你的 eBay 商品标题，提高搜索排名和点击率。")
        
        # 检查 eBay 授权状态
        oauth = shared_services.ebay_oauth
        if oauth and oauth.is_authorized():
            st.success("✅ eBay 已授权 - 可以使用完整功能")
        else:
            st.warning("⚠️ eBay 未授权 - 请先在主页面完成授权")
        
        st.divider()
        
        # 创建两列布局
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("📝 输入标题")
            original_title = st.text_area(
                "原始标题",
                placeholder="粘贴你的商品标题...",
                height=100
            )
            
            optimize_btn = st.button("🚀 优化标题", type="primary", use_container_width=True)
        
        with col2:
            st.subheader("✨ 优化结果")
            
            if optimize_btn and original_title:
                with st.spinner("AI 正在优化标题..."):
                    # 调用优化逻辑
                    optimized = self._optimize_title(original_title, shared_services)
                    
                    st.text_area(
                        "优化后标题",
                        value=optimized,
                        height=100
                    )
                    
                    # 显示字符数
                    st.caption(f"字符数: {len(optimized)}/80")
                    
                    # 保存到历史
                    self._save_history(original_title, optimized)
            else:
                st.info("输入标题后点击优化按钮")
        
        # 显示优化历史
        st.divider()
        st.subheader("📜 优化历史")
        self._render_history()
    
    def _optimize_title(self, title: str, shared_services: SharedServices) -> str:
        """
        使用 Qwen AI 优化标题
        """
        import os
        
        try:
            from qwen_optimizer import QwenOptimizer
            api_key = os.getenv("QWEN_API_KEY")
            if api_key:
                optimizer = QwenOptimizer(api_key=api_key)
                return optimizer.optimize_title(title, max_length=80)
        except Exception as e:
            st.warning(f"AI 优化失败，使用基础优化: {e}")
        
        # Fallback: 基础优化 (移除特殊字符，标题化)
        import re
        cleaned = re.sub(r'[^\w\s\-]', '', title)
        words = cleaned.split()
        
        # 保持80字符限制
        optimized = ' '.join(words)
        if len(optimized) > 80:
            optimized = optimized[:77] + "..."
        
        return optimized.title()
    
    def _save_history(self, original: str, optimized: str, score: float = None):
        """保存优化历史到插件数据库"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO optimization_history (original_title, optimized_title, score) VALUES (?, ?, ?)",
            (original, optimized, score)
        )
        conn.commit()
        conn.close()
    
    def _render_history(self):
        """渲染优化历史"""
        import sqlite3
        import pandas as pd
        
        if not self.db_path or not Path(self.db_path).exists():
            st.info("暂无优化历史")
            return
        
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql_query(
            "SELECT original_title, optimized_title, created_at FROM optimization_history ORDER BY id DESC LIMIT 10",
            conn
        )
        conn.close()
        
        if df.empty:
            st.info("暂无优化历史")
        else:
            st.dataframe(df, use_container_width=True)
    
    def get_sidebar_items(self):
        """侧边栏菜单项"""
        return ["单个优化", "批量优化", "优化历史"]
