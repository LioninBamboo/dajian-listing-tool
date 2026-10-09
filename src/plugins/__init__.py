"""
Dajian Listing Tool - Plugin System

插件系统允许第三方模块集成到主应用中，同时保持代码隔离。

使用方法:
1. 在 plugins/ 目录下创建你的插件文件夹
2. 实现 PluginBase 接口
3. 在 register_plugins() 中注册你的插件

示例:
    from src.plugins import PluginBase, register_plugin
    
    class MyPlugin(PluginBase):
        name = "My Plugin"
        description = "Does something cool"
        
        def render(self, shared_services):
            # 使用 Streamlit 渲染UI
            st.write("Hello from my plugin!")
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field


@dataclass
class SharedServices:
    """
    共享服务 - 传递给所有插件
    
    包含:
    - ebay_oauth: eBay OAuth 服务 (共享token)
    - db_path: 主数据库路径 (只读访问)
    - root_dir: 项目根目录
    """
    ebay_oauth: Any = None
    db_path: str = ""
    root_dir: str = ""
    environment: str = "PRODUCTION"
    
    # 插件专用数据库 (每个插件有自己的)
    plugin_db_paths: Dict[str, str] = field(default_factory=dict)


class PluginBase(ABC):
    """插件基类 - 所有插件必须继承此类"""
    
    # 插件元数据 (子类必须覆盖)
    name: str = "Unnamed Plugin"
    description: str = ""
    version: str = "1.0.0"
    icon: str = "🔌"
    
    # 插件是否需要自己的数据库
    requires_own_db: bool = False
    
    @abstractmethod
    def render(self, shared_services: SharedServices):
        """
        渲染插件UI (使用Streamlit)
        
        Args:
            shared_services: 共享服务对象，包含 eBay OAuth 等
        """
        pass
    
    def on_load(self, shared_services: SharedServices):
        """插件加载时调用 (可选覆盖)"""
        pass
    
    def on_unload(self):
        """插件卸载时调用 (可选覆盖)"""
        pass
    
    def get_sidebar_items(self) -> List[str]:
        """返回侧边栏菜单项 (可选覆盖)"""
        return []


# 全局插件注册表
_registered_plugins: Dict[str, PluginBase] = {}


def register_plugin(plugin_class: type):
    """
    注册插件的装饰器
    
    Usage:
        @register_plugin
        class MyPlugin(PluginBase):
            ...
    """
    if not issubclass(plugin_class, PluginBase):
        raise TypeError(f"{plugin_class.__name__} must inherit from PluginBase")
    
    instance = plugin_class()
    _registered_plugins[instance.name] = instance
    return plugin_class


def get_all_plugins() -> Dict[str, PluginBase]:
    """获取所有已注册的插件"""
    return _registered_plugins.copy()


def get_plugin(name: str) -> Optional[PluginBase]:
    """按名称获取插件"""
    return _registered_plugins.get(name)


def load_plugins_from_directory():
    """从 plugins 目录自动加载所有插件"""
    import importlib
    import pkgutil
    from pathlib import Path
    
    plugins_dir = Path(__file__).parent
    
    for _, name, is_pkg in pkgutil.iter_modules([str(plugins_dir)]):
        if is_pkg and name != "__pycache__":
            try:
                # 尝试导入插件模块
                module = importlib.import_module(f"src.plugins.{name}")
                print(f"✅ Loaded plugin: {name}")
            except Exception as e:
                print(f"⚠️ Failed to load plugin {name}: {e}")
