"""
优化历史管理器

用于追踪已优化的 eBay Item ID,确保每次优化不同的产品
"""
import json
import os
from datetime import datetime, timedelta
from typing import Set, Dict

class OptimizationHistory:
    """管理优化历史记录"""
    
    def __init__(self, history_file: str = "data/optimization_history.json"):
        self.history_file = history_file
        self.history: Dict[str, str] = {}  # {ItemID: last_optimized_date}
        self._load()
    
    def _load(self):
        """加载历史记录"""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.history = json.load(f)
            except Exception as e:
                print(f"⚠️ 加载历史记录失败: {e}")
                self.history = {}
        else:
            # 创建目录
            os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            self.history = {}
    
    def _save(self):
        """保存历史记录"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ 保存历史记录失败: {e}")
    
    def is_recently_optimized(self, item_id: str, days: int = 30) -> bool:
        """
        检查产品是否在最近 N 天内已优化
        
        Args:
            item_id: eBay Item ID
            days: 天数阈值(默认 30 天)
        
        Returns:
            True 如果最近已优化,False 如果需要优化
        """
        if item_id not in self.history:
            return False
        
        last_optimized = datetime.fromisoformat(self.history[item_id])
        cutoff = datetime.now() - timedelta(days=days)
        
        return last_optimized > cutoff
    
    def mark_optimized(self, item_id: str):
        """标记产品已优化"""
        self.history[item_id] = datetime.now().isoformat()
        self._save()
    
    def get_unoptimized_items(self, all_items: list[dict], days: int = 30) -> list[dict]:
        """
        从产品列表中过滤出未优化的产品
        
        Args:
            all_items: 所有产品列表 (包含 ItemID 字段)
            days: 天数阈值
        
        Returns:
            未优化的产品列表
        """
        unoptimized = []
        for item in all_items:
            item_id = item.get("ItemID")
            if item_id and not self.is_recently_optimized(item_id, days):
                unoptimized.append(item)
        
        return unoptimized
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        total = len(self.history)
        
        # 统计最近 30 天优化的数量
        cutoff_30 = datetime.now() - timedelta(days=30)
        recent_30 = sum(1 for date_str in self.history.values() 
                       if datetime.fromisoformat(date_str) > cutoff_30)
        
        # 统计最近 7 天优化的数量
        cutoff_7 = datetime.now() - timedelta(days=7)
        recent_7 = sum(1 for date_str in self.history.values() 
                      if datetime.fromisoformat(date_str) > cutoff_7)
        
        return {
            "total_optimized": total,
            "optimized_last_30_days": recent_30,
            "optimized_last_7_days": recent_7
        }
    
    def reset(self):
        """重置历史记录(慎用!)"""
        self.history = {}
        self._save()
        print("✅ 历史记录已重置")
