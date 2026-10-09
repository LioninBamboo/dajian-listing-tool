"""
市场趋势发掘服务 - Market Trend Discovery

核心功能:
1. 发现热门品类和趋势关键词
2. 分析 Item Specifics 需求
3. 匹配大建云仓收藏产品
4. 提供刊登建议

这是一个主动发掘功能，不依赖已有库存
"""
import sys
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime
import logging

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)


@dataclass
class TrendingCategory:
    """热门品类"""
    category_name: str
    category_id: str
    avg_price: float
    price_range: Tuple[float, float]
    competition_level: str
    demand_score: int  # 0-100
    sample_keywords: List[str] = field(default_factory=list)
    recommended_specs: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class MarketOpportunity:
    """市场机会"""
    keyword: str
    category: str
    market_avg_price: float
    competition: str
    demand_score: int
    opportunity_score: int
    suggested_price_range: Tuple[float, float]
    key_item_specifics: Dict[str, str]
    listing_tips: List[str]
    dajian_matches: List[Dict] = field(default_factory=list)  # 匹配的大建产品
    sell_through_rate: float = 0.0  # Sell-Through Rate 百分比 (0-100)
    ebay_search_url: str = ""  # eBay 搜索链接


class MarketTrendDiscovery:
    """
    市场趋势发掘引擎
    
    不依赖现有库存，主动发掘市场机会
    """
    
    # 家具类热门品类及其 eBay Category ID
    FURNITURE_CATEGORIES = {
        "sofas": {"id": "38208", "name": "Sofas", "min_price": 200},
        "sectional_sofas": {"id": "261599", "name": "Sectional Sofas", "min_price": 300},
        "beds": {"id": "175758", "name": "Beds & Bed Frames", "min_price": 150},
        "dining_tables": {"id": "38204", "name": "Dining Tables", "min_price": 100},
        "office_chairs": {"id": "25287", "name": "Office Chairs", "min_price": 50},
        "gaming_chairs": {"id": "171832", "name": "Gaming Chairs", "min_price": 80},
        "tv_stands": {"id": "20487", "name": "TV Stands", "min_price": 50},
        "coffee_tables": {"id": "38205", "name": "Coffee Tables", "min_price": 50},
        "dressers": {"id": "114397", "name": "Dressers & Chests", "min_price": 100},
        "bookcases": {"id": "3199", "name": "Bookcases", "min_price": 50},
        "kitchen_islands": {"id": "177003", "name": "Kitchen Islands", "min_price": 150},
        "vanities": {"id": "261598", "name": "Vanities", "min_price": 100},
    }
    
    # 高 Sell-Through Rate 类目关键词 (基于美国市场数据)
    # 这些是经过验证的高销量、高周转率品类
    HIGH_STR_KEYWORDS = {
        # 沙发类 - 高需求品类 (STR 15-25%)
        "sofas": [
            "modular sectional sofa",       # 模块化沙发，可配置性强
            "cloud couch dupe",             # 云朵沙发平替，网红款
            "velvet sofa",                  # 天鹅绒沙发，质感好
            "leather reclining sofa",       # 真皮躺椅沙发
            "sleeper sectional",            # 可睡觉沙发
        ],
        # 床类 - 稳定需求 (STR 12-20%)
        "beds": [
            "platform bed with storage",    # 储物床架
            "upholstered bed frame",        # 软包床架
            "LED bed frame",                # LED灯床架，年轻人喜欢
            "floating bed frame",           # 悬浮床架
            "king bed frame with headboard",# 带床头板大号床
        ],
        # 储物家具 - 高周转 (STR 18-28%)
        "storage": [
            "dresser with mirror",          # 带镜子梳妆台
            "6 drawer dresser",             # 六抽斗柜
            "tall dresser",                 # 高斗柜
            "nightstand with charging station",  # 带充电夜床头柜
            "closet organizer system",      # 衣柜收纳系统
        ],
        # 办公家具 - 持续增长 (STR 15-22%)
        "office": [
            "standing desk electric",       # 电动升降桌
            "L shaped desk",                # L型办公桌
            "executive desk",               # 行政办公桌
            "computer desk with hutch",     # 带书架电脑桌
            "ergonomic office chair",       # 人体工学椅
        ],
        # 户外家具 - 季节性高峰 (STR 20-35% 旺季)
        "outdoor": [
            "patio furniture set",          # 庭院家具套装
            "outdoor sectional sofa",       # 户外沙发
            "pergola with canopy",          # 凉亭
            "fire pit table",               # 火坑桌
            "outdoor dining set",           # 户外餐桌套装
        ],
        # 餐厅家具 - 稳定 (STR 12-18%)
        "dining": [
            "dining table set for 6",       # 6人餐桌套装
            "bar cabinet",                  # 酒柜
            "kitchen island with seating",  # 带座位厨房岛
            "buffet sideboard",             # 餐边柜
            "counter height dining set",    # 高脚餐桌套装
        ],
        # 娱乐家具 - 高转化 (STR 16-24%)
        "entertainment": [
            "TV stand with fireplace",      # 带壁炉电视柜
            "entertainment center",         # 娱乐中心
            "gaming desk",                  # 游戏桌
            "media console",                # 媒体柜
            "floating TV stand",            # 悬浮电视柜
        ],
    }
    
    # 各类目预估 STR 范围 (低值, 高值)
    CATEGORY_STR_RANGE = {
        "sofas": (18, 25),      # 沙发类高需求
        "beds": (14, 20),       # 床类稳定
        "storage": (20, 28),    # 储物家具高周转
        "office": (16, 22),     # 办公家具增长中
        "outdoor": (22, 35),    # 户外季节性高
        "dining": (14, 18),     # 餐厅稳定
        "entertainment": (18, 24),  # 娱乐高转化
    }
    
    @classmethod
    def get_estimated_str(cls, keyword: str, category: Optional[str] = None) -> float:
        """根据关键词或类目预估 STR%
        
        Args:
            keyword: 关键词
            category: 类目 (可选)
        
        Returns:
            预估 STR 百分比
        """
        # 如果指定了类目，直接用类目的范围
        if category and category in cls.CATEGORY_STR_RANGE:
            low, high = cls.CATEGORY_STR_RANGE[category]
            return (low + high) / 2  # 返回中间值
        
        # 根据关键词猜测类目
        keyword_lower = keyword.lower()
        for cat, keywords_list in cls.HIGH_STR_KEYWORDS.items():
            if keyword in keywords_list or any(kw in keyword_lower for kw in [cat.rstrip('s')]):
                if cat in cls.CATEGORY_STR_RANGE:
                    low, high = cls.CATEGORY_STR_RANGE[cat]
                    return (low + high) / 2
        
        # 默认 STR
        return 15.0
    
    @classmethod
    def generate_ebay_search_url(cls, keyword: str) -> str:
        """生成 eBay 搜索链接
        
        Args:
            keyword: 搜索关键词
        
        Returns:
            eBay 搜索 URL
        """
        from urllib.parse import quote_plus
        encoded_keyword = quote_plus(keyword)
        return f"https://www.ebay.com/sch/i.html?_nkw={encoded_keyword}&_sacat=0&LH_BIN=1"
    
    @classmethod
    def get_high_str_keywords(cls, category: Optional[str] = None) -> List[str]:
        """获取高 Sell-Through Rate 关键词
        
        Args:
            category: 具体类目 (sofas/beds/storage/office/outdoor/dining/entertainment)
                     None 则返回所有类目的关键词
        
        Returns:
            关键词列表
        """
        if category and category in cls.HIGH_STR_KEYWORDS:
            return cls.HIGH_STR_KEYWORDS[category]
        
        # 返回所有类目的关键词
        all_keywords = []
        for keywords in cls.HIGH_STR_KEYWORDS.values():
            all_keywords.extend(keywords)
        return all_keywords
    
    # 高需求 Item Specifics 模板
    RECOMMENDED_ITEM_SPECIFICS = {
        "sofas": {
            "Type": ["Sectional", "Loveseat", "Sleeper Sofa", "Reclining Sofa"],
            "Material": ["Leather", "Faux Leather", "Velvet", "Linen", "Microfiber"],
            "Seating Capacity": ["2", "3", "4", "5+"],
            "Features": ["Reclining", "Storage", "Convertible", "Modular"],
            "Color": ["Gray", "Black", "Brown", "Beige", "Blue", "Green"],
        },
        "beds": {
            "Size": ["Twin", "Full", "Queen", "King", "California King"],
            "Type": ["Platform Bed", "Panel Bed", "Canopy Bed", "Storage Bed"],
            "Material": ["Wood", "Metal", "Upholstered", "Faux Leather"],
            "Style": ["Modern", "Mid-Century", "Farmhouse", "Industrial"],
            "Features": ["Storage", "Headboard Included", "LED Lights"],
        },
        "chairs": {
            "Type": ["Gaming Chair", "Office Chair", "Accent Chair", "Dining Chair"],
            "Material": ["Mesh", "Leather", "Fabric", "PU Leather"],
            "Features": ["Lumbar Support", "Adjustable", "Reclining", "Swivel"],
            "Color": ["Black", "Gray", "Blue", "Red", "White"],
        },
        "tables": {
            "Type": ["Coffee Table", "Dining Table", "Console Table", "End Table"],
            "Material": ["Wood", "Glass", "Metal", "Marble", "MDF"],
            "Shape": ["Rectangle", "Round", "Square", "Oval"],
            "Style": ["Modern", "Rustic", "Industrial", "Farmhouse"],
        },
    }
    
    def __init__(self):
        self._terapeak = None
        self._dajian = None
    
    @property
    def terapeak(self):
        """延迟加载 TerapeakClient"""
        if self._terapeak is None:
            from src.plugins.terapeak_research.research_client import TerapeakClient
            self._terapeak = TerapeakClient()
        return self._terapeak
    
    @property
    def dajian(self):
        """延迟加载 DaJianClient"""
        if self._dajian is None:
            from src.clients.dajian_client import DaJianClient
            client_id = os.getenv("DAJIAN_API_KEY")
            client_secret = os.getenv("DAJIAN_API_SECRET")
            if client_id and client_secret:
                self._dajian = DaJianClient(client_id, client_secret)
            else:
                logger.warning("大建API凭证未配置")
        return self._dajian
    
    def discover_trending_categories(self, limit: int = 10) -> List[TrendingCategory]:
        """
        发现热门品类
        
        Returns:
            按需求分数排序的热门品类列表
        """
        trending = []
        
        for key, cat_info in list(self.FURNITURE_CATEGORIES.items())[:limit]:
            try:
                # 搜索该品类的市场数据
                products = self.terapeak.search_products(
                    cat_info["name"],
                    limit=50,
                    min_price=cat_info["min_price"],
                    buy_it_now_only=True
                )
                
                if not products:
                    continue
                
                prices = [p['price'] for p in products if p.get('price', 0) > 0]
                
                if not prices:
                    continue
                
                avg_price = sum(prices) / len(prices)
                min_price = min(prices)
                max_price = max(prices)
                
                # 评估竞争和需求
                competition = "high" if len(products) >= 40 else "medium" if len(products) >= 20 else "low"
                demand_score = min(100, len(products) * 2 + 20)  # 简单评估
                
                # 获取推荐的 Item Specifics
                specs = self._get_category_specs(key)
                
                trending.append(TrendingCategory(
                    category_name=cat_info["name"],
                    category_id=cat_info["id"],
                    avg_price=round(avg_price, 2),
                    price_range=(round(min_price, 2), round(max_price, 2)),
                    competition_level=competition,
                    demand_score=demand_score,
                    sample_keywords=[cat_info["name"], key.replace("_", " ")],
                    recommended_specs=specs
                ))
                
            except Exception as e:
                logger.error(f"分析品类 {key} 失败: {e}")
                continue
        
        # 按需求分数排序
        trending.sort(key=lambda x: x.demand_score, reverse=True)
        
        return trending
    
    def discover_opportunities(self, keywords: Optional[List[str]] = None, 
                              include_dajian_favorites: bool = True,
                              min_margin: float = 0.20) -> List[MarketOpportunity]:
        """
        发现市场机会
        
        Args:
            keywords: 要分析的关键词 (None 则使用默认热门关键词)
            include_dajian_favorites: 是否检查大建收藏产品
            min_margin: 最低利润率要求
        
        Returns:
            市场机会列表
        """
        if keywords is None:
            keywords = [
                "sectional sofa",
                "platform bed",
                "gaming chair", 
                "coffee table",
                "kitchen island",
                "tv stand",
                "bookshelf",
                "vanity desk",
                "recliner sofa",
                "dining table set"
            ]
        
        opportunities = []
        
        for kw in keywords:
            try:
                opp = self._analyze_keyword_opportunity(kw, min_margin)
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                logger.error(f"分析关键词 '{kw}' 失败: {e}")
                continue
        
        # 如果启用，尝试获取大建收藏产品并匹配
        if include_dajian_favorites and self.dajian:
            try:
                favorites = self.dajian.get_favorites()
                if favorites:
                    self._match_dajian_products(opportunities, favorites)
            except Exception as e:
                logger.warning(f"获取大建收藏失败: {e}")
        
        # 按机会分数排序
        opportunities.sort(key=lambda x: x.opportunity_score, reverse=True)
        
        return opportunities
    
    def _analyze_keyword_opportunity(self, keyword: str, 
                                    min_margin: float) -> Optional[MarketOpportunity]:
        """分析单个关键词的市场机会"""
        # 获取市场数据 + eBay 真实 active total
        result = self.terapeak.search_products_with_total(
            keywords=keyword,
            limit=50,
            min_price=self._estimate_min_price(keyword),
            buy_it_now_only=True,
        )
        products = result.get('items', [])
        active_total = int(result.get('total') or 0)
        
        if not products:
            return None
        
        prices = [p['price'] for p in products if p.get('price', 0) > 0]
        
        if len(prices) < 5:
            return None
        
        avg_price = sum(prices) / len(prices)
        min_price = min(prices)
        max_price = max(prices)
        
        # 评估竞争 - 走统一分类器
        from .scoring import classify_competition, opportunity_score, ScoringInputs
        competition, _ = classify_competition(active_total=active_total, sample_size=len(products))
        
        # 价格分散度
        price_variance = sum((p - avg_price) ** 2 for p in prices) / len(prices)
        price_stability = 1 - min(1, (price_variance ** 0.5) / avg_price)
        price_spread = (price_variance ** 0.5) / avg_price if avg_price > 0 else 0
        # 需求分数: 真实 active_total + 价格稳定性 (上限 100)
        demand_score = min(100, int((active_total or len(products)) // 50 + price_stability * 50))

        # 估算 STR (走类目区间), 这里没有真实 sold 数据
        estimated_str = self.get_estimated_str(keyword)

        # 评估需求分数 (基于商品数量和价格稳定性)
        # 这里没有 cost/margin 上下文 → 用 min_margin 作占位 (UI/auto_discover 路径才会真有 margin)
        opp_score_value = opportunity_score(ScoringInputs(
            margin_rate=min_margin,
            competition_level=competition,  # type: ignore[arg-type]
            price_spread=price_spread,
            estimated_str=estimated_str,
        ))
        
        # 建议售价区间 (考虑利润率)
        suggested_min = min_price * 0.9
        suggested_max = avg_price * 1.1
        
        # 获取关键 Item Specifics
        key_specs = self._get_keyword_specs(keyword)
        
        # 生成刊登建议
        tips = self._generate_listing_tips(keyword, competition, avg_price)
        
        # 获取预估 STR 和 eBay 搜索链接
        category = self._guess_category(keyword)
        ebay_url = self.generate_ebay_search_url(keyword)
        
        return MarketOpportunity(
            keyword=keyword,
            category=category,
            market_avg_price=round(avg_price, 2),
            competition=competition,
            demand_score=demand_score,
            opportunity_score=opp_score_value,
            suggested_price_range=(round(suggested_min, 2), round(suggested_max, 2)),
            key_item_specifics=key_specs,
            listing_tips=tips,
            dajian_matches=[],
            sell_through_rate=estimated_str,
            ebay_search_url=ebay_url
        )
    
    def _estimate_min_price(self, keyword: str) -> float:
        """根据关键词估算最低有效价格"""
        keyword_lower = keyword.lower()
        
        if any(w in keyword_lower for w in ['sofa', 'sectional', 'couch']):
            return 150
        elif any(w in keyword_lower for w in ['bed', 'platform', 'frame']):
            return 100
        elif any(w in keyword_lower for w in ['table', 'desk', 'island']):
            return 80
        elif any(w in keyword_lower for w in ['chair']):
            return 50
        else:
            return 30
    
    def _get_category_specs(self, category_key: str) -> Dict[str, List[str]]:
        """获取品类的推荐 Item Specifics"""
        # 映射到模板
        if "sofa" in category_key or "sectional" in category_key:
            return self.RECOMMENDED_ITEM_SPECIFICS.get("sofas", {})
        elif "bed" in category_key:
            return self.RECOMMENDED_ITEM_SPECIFICS.get("beds", {})
        elif "chair" in category_key:
            return self.RECOMMENDED_ITEM_SPECIFICS.get("chairs", {})
        elif "table" in category_key or "stand" in category_key:
            return self.RECOMMENDED_ITEM_SPECIFICS.get("tables", {})
        else:
            return {}
    
    def _get_keyword_specs(self, keyword: str) -> Dict[str, str]:
        """获取关键词对应的推荐 Item Specifics (单值)"""
        keyword_lower = keyword.lower()
        specs = {}
        
        # 沙发类
        if "sectional" in keyword_lower:
            specs = {"Type": "Sectional", "Seating Capacity": "5+", "Features": "Modular"}
        elif "recliner" in keyword_lower or "reclining" in keyword_lower:
            specs = {"Type": "Reclining Sofa", "Features": "Reclining"}
        elif "cloud" in keyword_lower or "modular" in keyword_lower:
            specs = {"Type": "Modular Sofa", "Features": "Modular, Configurable"}
        elif "sleeper" in keyword_lower:
            specs = {"Type": "Sleeper Sofa", "Features": "Convertible"}
        elif "sofa" in keyword_lower or "couch" in keyword_lower:
            specs = {"Type": "Sofa"}
        
        # 床类
        elif "platform bed" in keyword_lower:
            specs = {"Type": "Platform Bed", "Features": "No Box Spring Needed"}
        elif "upholstered bed" in keyword_lower:
            specs = {"Type": "Panel Bed", "Material": "Upholstered"}
        elif "led bed" in keyword_lower:
            specs = {"Type": "Platform Bed", "Features": "LED Lights"}
        elif "floating bed" in keyword_lower:
            specs = {"Type": "Platform Bed", "Style": "Modern"}
        elif "storage bed" in keyword_lower or "bed with storage" in keyword_lower:
            specs = {"Type": "Storage Bed", "Features": "Storage"}
        elif "bed frame" in keyword_lower or "bed" in keyword_lower:
            specs = {"Type": "Bed Frame"}
        
        # 储物类
        elif "dresser" in keyword_lower:
            specs = {"Type": "Dresser", "Material": "Wood"}
        elif "nightstand" in keyword_lower:
            specs = {"Type": "Nightstand", "Features": "USB Charging" if "charging" in keyword_lower else "Storage"}
        elif "closet organizer" in keyword_lower:
            specs = {"Type": "Closet Organizer"}
        
        # 办公类
        elif "standing desk" in keyword_lower:
            specs = {"Type": "Standing Desk", "Features": "Electric Height Adjustable"}
        elif "l shaped desk" in keyword_lower or "l-shaped desk" in keyword_lower:
            specs = {"Type": "L-Shaped Desk", "Features": "Corner Design"}
        elif "executive desk" in keyword_lower:
            specs = {"Type": "Executive Desk", "Style": "Traditional"}
        elif "computer desk" in keyword_lower:
            specs = {"Type": "Computer Desk"}
        elif "gaming desk" in keyword_lower:
            specs = {"Type": "Gaming Desk", "Features": "Cable Management, RGB"}
        elif "gaming chair" in keyword_lower:
            specs = {"Type": "Gaming Chair", "Features": "Lumbar Support, Adjustable"}
        elif "office chair" in keyword_lower or "ergonomic" in keyword_lower:
            specs = {"Type": "Office Chair", "Features": "Ergonomic, Swivel"}
        
        # 户外类
        elif "patio" in keyword_lower or "outdoor" in keyword_lower:
            specs = {"Type": "Patio Set", "Material": "Weather Resistant"}
        elif "pergola" in keyword_lower:
            specs = {"Type": "Pergola", "Features": "Canopy"}
        elif "fire pit" in keyword_lower:
            specs = {"Type": "Fire Pit Table", "Fuel Type": "Propane"}
        
        # 餐厅类
        elif "dining table" in keyword_lower or "dining set" in keyword_lower:
            specs = {"Type": "Dining Table", "Shape": "Rectangle"}
        elif "bar cabinet" in keyword_lower:
            specs = {"Type": "Bar Cabinet", "Features": "Wine Storage"}
        elif "kitchen island" in keyword_lower:
            specs = {"Type": "Kitchen Island", "Features": "Storage"}
        elif "sideboard" in keyword_lower or "buffet" in keyword_lower:
            specs = {"Type": "Sideboard/Buffet", "Features": "Storage"}
        elif "counter height" in keyword_lower:
            specs = {"Type": "Counter Height Table"}
        
        # 娱乐类
        elif "tv stand" in keyword_lower and "fireplace" in keyword_lower:
            specs = {"Type": "TV Stand", "Features": "Electric Fireplace"}
        elif "tv stand" in keyword_lower:
            specs = {"Type": "TV Stand", "Features": "Cable Management"}
        elif "entertainment center" in keyword_lower:
            specs = {"Type": "Entertainment Center"}
        elif "media console" in keyword_lower:
            specs = {"Type": "Media Console"}
        elif "floating tv" in keyword_lower:
            specs = {"Type": "Floating TV Stand", "Mounting": "Wall Mount"}
        
        # 其他
        elif "coffee table" in keyword_lower:
            specs = {"Type": "Coffee Table", "Shape": "Rectangle"}
        elif "bookshelf" in keyword_lower:
            specs = {"Type": "Bookcase", "Material": "Wood"}
        elif "vanity" in keyword_lower:
            specs = {"Type": "Vanity", "Features": "Mirror Included" if "mirror" in keyword_lower else "Storage"}
        
        # 通用规格
        if "Material" not in specs:
            if "leather" in keyword_lower:
                specs["Material"] = "Leather"
            elif "velvet" in keyword_lower:
                specs["Material"] = "Velvet"
            elif "wood" in keyword_lower:
                specs["Material"] = "Wood"
            elif "metal" in keyword_lower:
                specs["Material"] = "Metal"
        
        # 尺寸
        if "Size" not in specs:
            if "king" in keyword_lower:
                specs["Size"] = "King"
            elif "queen" in keyword_lower:
                specs["Size"] = "Queen"
            elif "full" in keyword_lower:
                specs["Size"] = "Full"
            elif "twin" in keyword_lower:
                specs["Size"] = "Twin"
        
        return specs
    
    def _guess_category(self, keyword: str) -> str:
        """根据关键词猜测品类"""
        keyword_lower = keyword.lower()
        
        category_map = {
            # 沙发类
            "sectional": "Sectional Sofas",
            "modular sofa": "Sectional Sofas",
            "cloud": "Sectional Sofas",
            "sleeper": "Sofas",
            "reclining": "Sofas",
            "sofa": "Sofas",
            "couch": "Sofas",
            
            # 床类
            "platform bed": "Beds & Bed Frames",
            "bed frame": "Beds & Bed Frames",
            "upholstered bed": "Beds & Bed Frames",
            "led bed": "Beds & Bed Frames",
            "floating bed": "Beds & Bed Frames",
            "storage bed": "Beds & Bed Frames",
            "bed": "Beds & Bed Frames",
            
            # 储物类
            "dresser": "Dressers & Chests",
            "nightstand": "Nightstands",
            "closet organizer": "Closet Organizers",
            
            # 办公类
            "standing desk": "Desks",
            "l shaped desk": "Desks",
            "executive desk": "Desks",
            "computer desk": "Desks",
            "gaming desk": "Desks",
            "desk": "Desks",
            "gaming chair": "Gaming Chairs",
            "office chair": "Office Chairs",
            "ergonomic chair": "Office Chairs",
            "chair": "Chairs",
            
            # 户外类
            "patio": "Patio & Garden Furniture",
            "outdoor": "Patio & Garden Furniture",
            "pergola": "Gazebos",
            "fire pit": "Fire Pits & Chimineas",
            
            # 餐厅类
            "dining": "Dining Furniture Sets",
            "bar cabinet": "Home Bars & Bar Furniture",
            "kitchen island": "Kitchen Islands",
            "sideboard": "Sideboards & Buffets",
            "buffet": "Sideboards & Buffets",
            "counter height": "Dining Tables",
            
            # 娱乐类
            "tv stand": "TV Stands",
            "entertainment center": "Entertainment Units",
            "media console": "Entertainment Units",
            
            # 其他
            "coffee table": "Coffee Tables",
            "table": "Tables",
            "bookshelf": "Bookcases",
            "vanity": "Vanities",
            "island": "Kitchen Islands"
        }
        
        for key, cat in category_map.items():
            if key in keyword_lower:
                return cat
        
        return "Home & Garden"
    
    def _generate_listing_tips(self, keyword: str, competition: str, 
                              avg_price: float) -> List[str]:
        """生成刊登建议"""
        tips = []
        
        # 竞争建议
        if competition == "very_high":
            tips.append("⚠️ 竞争激烈：建议突出差异化卖点（材质、功能、设计）")
            tips.append("💡 考虑长尾关键词优化，如添加颜色、材质、尺寸")
        elif competition == "low":
            tips.append("✅ 竞争较小：好的进入时机，可以适当提高定价")
        
        # 定价建议
        if avg_price > 500:
            tips.append("💰 高客单价品类：重视产品描述和图片质量")
            tips.append("🚚 考虑提供免费送货提升转化率")
        elif avg_price < 100:
            tips.append("📦 低客单价：控制成本是关键，考虑批量采购")
        
        # 关键词建议
        keyword_lower = keyword.lower()
        if "sofa" in keyword_lower:
            tips.append("📝 标题建议包含：尺寸(如108\")、座位数、材质、颜色")
            tips.append("🖼️ 主图建议：展示多角度，强调客厅场景")
        elif "chair" in keyword_lower:
            tips.append("📝 标题建议包含：类型、材质、可调节功能")
            tips.append("🖼️ 主图建议：展示人体工学设计和舒适性")
        elif "table" in keyword_lower:
            tips.append("📝 标题建议包含：尺寸、形状、材质、风格")
        
        return tips
    
    def _match_dajian_products(self, opportunities: List[MarketOpportunity], 
                              dajian_products: List[Dict]):
        """将大建产品匹配到市场机会"""
        for opp in opportunities:
            kw_lower = opp.keyword.lower()
            kw_words = set(kw_lower.split())
            
            for product in dajian_products:
                title = product.get("title", "") or product.get("name", "")
                title_lower = title.lower()
                
                # 计算匹配度
                title_words = set(title_lower.split())
                match_count = len(kw_words & title_words)
                
                if match_count >= len(kw_words) * 0.5:  # 至少匹配一半关键词
                    images = product.get("images") or []
                    if not isinstance(images, list):
                        images = []
                    image_url = ""
                    if images and isinstance(images[0], str):
                        image_url = images[0]
                    if not image_url:
                        for k in ("image_url", "imageUrl", "picUrl", "mainImage"):
                            v = product.get(k)
                            if isinstance(v, str) and v.startswith("http"):
                                image_url = v
                                break
                    opp.dajian_matches.append({
                        "sku": product.get("sku", product.get("skuCode", "")),
                        "title": title,
                        "price": product.get("price", 0),
                        "url": product.get("url", product.get("detailUrl", "")),
                        "image_url": image_url,
                    })
    
    def get_listing_recommendation(self, keyword: str) -> Dict:
        """
        获取完整的刊登建议
        
        Returns:
            包含标题建议、Item Specifics、定价策略的完整建议
        """
        opp = self._analyze_keyword_opportunity(keyword, 0.15)
        
        if not opp:
            return {"error": f"无法分析关键词: {keyword}"}
        
        # 生成标题模板
        title_templates = self._generate_title_templates(keyword, opp)
        
        # Item Specifics 建议
        specs = self._get_keyword_specs(keyword)
        full_specs = self._get_category_specs(keyword.split()[0])
        
        return {
            "keyword": keyword,
            "category": opp.category,
            "market_analysis": {
                "avg_price": opp.market_avg_price,
                "price_range": opp.suggested_price_range,
                "competition": opp.competition,
                "demand_score": opp.demand_score,
            },
            "title_templates": title_templates,
            "required_item_specifics": specs,
            "recommended_item_specifics": full_specs,
            "pricing_strategy": {
                "floor_price": opp.suggested_price_range[0],
                "ceiling_price": opp.suggested_price_range[1],
                "recommended_price": round((opp.suggested_price_range[0] + opp.suggested_price_range[1]) / 2, 2),
            },
            "listing_tips": opp.listing_tips,
            "opportunity_score": opp.opportunity_score,
        }
    
    def _generate_title_templates(self, keyword: str, opp: MarketOpportunity) -> List[str]:
        """生成标题模板"""
        base = keyword.title()
        
        templates = [
            f"[Size]\" {base} [Color] [Material] - [Feature] for Living Room",
            f"{base} [Material] [Color] [Size] Seater with [Feature]",
            f"Modern {base} - [Material] [Color] [Feature] - Free Shipping",
        ]
        
        # 根据品类调整
        if "sofa" in keyword.lower():
            templates.append(f"[Size]\" {base} Couch [Material] [Color] - [Seating]+ Seat Sectional")
        elif "chair" in keyword.lower():
            templates.append(f"{base} - [Material] [Feature] - Ergonomic [Color] Computer Chair")
        elif "table" in keyword.lower():
            templates.append(f"{base} [Material] [Shape] [Size]\" - [Style] [Feature]")
        
        return templates


# 便捷函数
def discover_opportunities(keywords: Optional[List[str]] = None) -> List[Dict]:
    """快速发现市场机会"""
    service = MarketTrendDiscovery()
    opportunities = service.discover_opportunities(keywords)
    return [asdict(opp) for opp in opportunities]


def get_listing_advice(keyword: str) -> Dict:
    """获取刊登建议"""
    service = MarketTrendDiscovery()
    return service.get_listing_recommendation(keyword)
