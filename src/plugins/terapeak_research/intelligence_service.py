"""
Market Intelligence Service - 智能决策引擎

核心功能:
1. Reverse Sourcing (反向选品) - 从热门关键词匹配大建库存
2. Market Analysis (市场水位分析) - 分析价格、竞争强度
3. Competitor Pain Points (竞品痛点发掘) - 提取差评关键词
4. Smart Recommendations (智能推荐) - 综合评分推荐刊登
"""
import sys
import os
import json
import sqlite3
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from decimal import Decimal

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")


@dataclass
class MarketAnalysis:
    """市场水位分析结果"""
    keyword: str
    avg_price: float
    min_price: float
    max_price: float
    median_price: float
    total_listings: int          # sample size returned by API (受 limit 限制)
    competition_level: str       # low, medium, high, very_high
    competition_score: int       # 0-100, higher = more competitive
    price_spread: float          # std/mean of filtered prices
    active_total: int = 0        # eBay-reported total active listings (real population)
    demand_signal_score: int = 0           # F2-pivot: 0-100 Browse-only proxy (MI denied)
    demand_signal_label: str = "需求未知"   # short Chinese label
    analyzed_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProductOpportunity:
    """产品机会评估"""
    sku: str
    title: str
    dajian_cost: float
    market_avg_price: float
    potential_margin: float
    margin_rate: float
    competition_level: str
    opportunity_score: int  # 0-100
    recommendation: str
    smart_price: float
    strategy: str
    matched_keywords: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompetitorPainPoint:
    """竞品痛点"""
    issue: str
    frequency: int
    our_advantage: str
    suggested_emphasis: str


class IntelligenceService:
    """
    市场智能决策引擎
    
    整合 Terapeak 数据、大建库存、定价引擎，提供智能决策支持
    """
    
    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(PROJECT_ROOT / "ebay_collection.db")
        self._terapeak_client = None
        self._pricing_engine = None
    
    @property
    def terapeak(self):
        """延迟加载 TerapeakClient"""
        if self._terapeak_client is None:
            from .research_client import TerapeakClient
            self._terapeak_client = TerapeakClient()
        return self._terapeak_client
    
    @property
    def pricing(self):
        """延迟加载 PricingEngine"""
        if self._pricing_engine is None:
            from src.services.pricing_engine import PricingEngine
            self._pricing_engine = PricingEngine
        return self._pricing_engine
    
    # ========== 1. Market Analysis (市场水位分析) ==========
    
    def analyze_market(self, keyword: str, category_id: str = None, 
                      min_price: float = None) -> MarketAnalysis:
        """
        分析市场水位
        
        Args:
            keyword: 搜索关键词
            category_id: 可选的分类限制
            min_price: 最低价格过滤（用于过滤拍卖起始价等虚假低价）
        
        Returns:
            MarketAnalysis 对象，包含价格、竞争等分析
        """
        # 根据关键词智能推断最低价格过滤
        # 家具类产品一般最低$50起
        if min_price is None:
            keyword_lower = keyword.lower()
            if any(w in keyword_lower for w in ['sofa', 'sectional', 'couch', 'bed', 'table', 'dresser', 'cabinet']):
                min_price = 100  # 大件家具
            elif any(w in keyword_lower for w in ['chair', 'stool', 'bench', 'ottoman']):
                min_price = 50   # 椅子类
            else:
                min_price = 20   # 其他
        
        # 获取市场数据 - 使用 Buy It Now 筛选和最低价过滤
        # 用 _with_total 拿到 eBay 实际报告的 total 而不是只看样本长度
        result = self.terapeak.search_products_with_total(
            keywords=keyword,
            category_id=category_id,
            limit=100,
            sort="bestMatch",
            min_price=min_price,
            buy_it_now_only=True,
        )
        products = result.get('items', [])
        active_total = int(result.get('total') or 0)
        
        if not products:
            return MarketAnalysis(
                keyword=keyword,
                avg_price=0, min_price=0, max_price=0, median_price=0,
                total_listings=0, competition_level="unknown",
                competition_score=0, price_spread=0, active_total=active_total,
            )
        
        # 提取价格数据
        prices = [p['price'] for p in products if p.get('price', 0) > 0]
        
        if not prices:
            return MarketAnalysis(
                keyword=keyword,
                avg_price=0, min_price=0, max_price=0, median_price=0,
                total_listings=len(products), competition_level="unknown",
                competition_score=0, price_spread=0, active_total=active_total,
            )
        
        # 进一步过滤异常值 - 使用 IQR 方法
        sorted_prices = sorted(prices)
        q1_idx = len(sorted_prices) // 4
        q3_idx = 3 * len(sorted_prices) // 4
        q1 = sorted_prices[q1_idx] if q1_idx < len(sorted_prices) else sorted_prices[0]
        q3 = sorted_prices[q3_idx] if q3_idx < len(sorted_prices) else sorted_prices[-1]
        iqr = q3 - q1
        
        # 过滤掉低于 Q1 - 1.5*IQR 或高于 Q3 + 1.5*IQR 的异常值
        lower_bound = max(0, q1 - 1.5 * iqr)
        upper_bound = q3 + 1.5 * iqr
        filtered_prices = [p for p in prices if lower_bound <= p <= upper_bound]
        
        # 如果过滤后数据太少，使用原始数据
        if len(filtered_prices) < 5:
            filtered_prices = prices
        
        # 计算统计数据
        avg_price = sum(filtered_prices) / len(filtered_prices)
        min_price_stat = min(filtered_prices)
        max_price_stat = max(filtered_prices)
        sorted_filtered = sorted(filtered_prices)
        median_price = sorted_filtered[len(sorted_filtered) // 2]
        
        # 价格分散度 (变异系数)
        variance = sum((p - avg_price) ** 2 for p in filtered_prices) / len(filtered_prices)
        std_dev = variance ** 0.5
        price_spread = std_dev / avg_price if avg_price > 0 else 0
        
        # 竞争强度评估 - 走统一分类器，优先用 eBay 真实 active_total
        from .scoring import classify_competition, compute_demand_signal
        competition_level, competition_score = classify_competition(
            active_total=active_total,
            sample_size=len(products),
        )

        # F2-pivot: Marketplace Insights API access denied by eBay, so we
        # build a transparent Browse-only demand proxy instead of pretending
        # we have real STR.
        try:
            from .trend_discovery import TrendDiscovery
            est_str = TrendDiscovery.get_estimated_str(keyword)
        except Exception:
            est_str = None
        demand = compute_demand_signal(
            active_total=active_total,
            sample_size=len(products),
            price_spread=price_spread,
            estimated_str=est_str,
        )

        return MarketAnalysis(
            keyword=keyword,
            avg_price=round(avg_price, 2),
            min_price=round(min_price_stat, 2),
            max_price=round(max_price_stat, 2),
            median_price=round(median_price, 2),
            total_listings=len(products),
            competition_level=competition_level,
            competition_score=competition_score,
            price_spread=round(price_spread, 3),
            active_total=active_total,
            demand_signal_score=demand.score,
            demand_signal_label=demand.label,
        )
    
    # ========== 2. Reverse Sourcing (反向选品) ==========
    
    def reverse_source(self, hot_keywords: List[str], 
                      min_margin: float = 0.15,
                      max_results: int = 20) -> List[ProductOpportunity]:
        """
        反向选品 - 从热门关键词找到高潜力库存产品
        
        Args:
            hot_keywords: 热门关键词列表
            min_margin: 最低利润率要求
            max_results: 最大返回数量
        
        Returns:
            按机会分数排序的产品列表
        """
        opportunities = []
        
        # 获取本地库存产品
        products = self._get_local_inventory()
        
        for keyword in hot_keywords:
            # 获取市场数据
            market = self.analyze_market(keyword)
            
            if market.avg_price <= 0:
                continue
            
            # 匹配产品
            for product in products:
                if self._keyword_matches_product(keyword, product):
                    opp = self._evaluate_opportunity(product, market, keyword, min_margin)
                    if opp and opp.margin_rate >= min_margin:
                        opportunities.append(opp)
        
        # 去重 (同一 SKU 可能匹配多个关键词)
        unique_opps = {}
        for opp in opportunities:
            if opp.sku not in unique_opps or opp.opportunity_score > unique_opps[opp.sku].opportunity_score:
                unique_opps[opp.sku] = opp
        
        # 按机会分数排序
        sorted_opps = sorted(unique_opps.values(), key=lambda x: x.opportunity_score, reverse=True)
        
        return sorted_opps[:max_results]
    
    def _load_mi_blacklist(self) -> set:
        """F1 — 读取 Streamlit MI 页面维护的 SKU 屏蔽名单。
        
        与 src/web/pages/market_intelligence.py 共用 reports/mi_blacklist.json。
        文件不存在或解析失败时返回空 set，不影响主流程。

        F1.1 — 兼容 dict 形态 + TTL：
        - list 形态视作永久条目；
        - dict 形态自动剥离已过期项（不回写文件，由 UI 端持久化）。
        """
        try:
            blacklist_path = PROJECT_ROOT / "reports" / "mi_blacklist.json"
            if not blacklist_path.exists():
                return set()
            data = json.loads(blacklist_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(x) for x in data if x}
            if isinstance(data, dict):
                from datetime import datetime as _dt
                now = _dt.now()
                active: set = set()
                for sku, meta in data.items():
                    if not sku:
                        continue
                    meta = meta if isinstance(meta, dict) else {}
                    exp = meta.get("expires_at")
                    if exp:
                        try:
                            if _dt.fromisoformat(exp) < now:
                                continue
                        except Exception:
                            pass
                    active.add(str(sku))
                return active
        except Exception:
            pass
        return set()
    
    def _get_local_inventory(self) -> List[Dict]:
        """获取本地大建库存产品"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        # 只扫描未刊登候选：PENDING / COLLECTED / READY。
        # PUBLISHED 已经在线，不应再进入“推荐刊登”或 MI 日报候选池。
        cur.execute("""
            SELECT sku, title, url, cost_breakdown, status, images, listing_id
            FROM collected_products 
            WHERE status IN ('PENDING', 'COLLECTED', 'READY')
              AND cost_breakdown IS NOT NULL
        """)
        
        products = []
        for row in cur.fetchall():
            cost_data = json.loads(row['cost_breakdown']) if row['cost_breakdown'] else {}
            try:
                images = json.loads(row['images']) if row['images'] else []
            except (TypeError, ValueError):
                images = []
            if not isinstance(images, list):
                images = []
            products.append({
                'sku': row['sku'],
                'title': row['title'],
                'url': row['url'],
                'status': row['status'],
                'images': images,
                'image_url': images[0] if images else '',
                'product_price': cost_data.get('product_price', 0),
                'shipping_cost': cost_data.get('shipping_cost', 0),
                'total_cost': cost_data.get('total_dajian_cost', 0),
                'listing_id': row['listing_id'] or '',
            })
        
        conn.close()
        return products
    
    def _keyword_matches_product(self, keyword: str, product: Dict) -> bool:
        """检查关键词是否匹配产品"""
        keyword_lower = keyword.lower()
        title_lower = product.get('title', '').lower()
        
        # 分词匹配
        kw_words = keyword_lower.split()
        
        # 至少匹配 50% 的关键词
        matched = sum(1 for word in kw_words if word in title_lower)
        return matched >= len(kw_words) * 0.5
    
    def _evaluate_opportunity(self, product: Dict, market: MarketAnalysis, 
                             keyword: str, min_margin: float) -> Optional[ProductOpportunity]:
        """评估产品机会"""
        total_cost = product.get('total_cost', 0)
        
        if total_cost <= 0 or market.avg_price <= 0:
            return None
        
        # 计算智能定价
        smart_price_result = self.calculate_smart_price(
            total_cost=total_cost,
            market_price=market.avg_price,
            min_margin=min_margin,
            max_margin=0.35
        )
        
        smart_price = smart_price_result['final_price']
        strategy = smart_price_result['strategy']
        
        # 真实净利润率 (已扣 eBay 费 + 广告 + 折扣 + 固定费), PricingEngine 直接返回
        margin_rate = float(smart_price_result.get('margin', 0))
        potential_margin = total_cost * margin_rate
        
        # 计算机会分数
        score = self._calculate_opportunity_score(
            margin_rate=margin_rate,
            competition_level=market.competition_level,
            price_spread=market.price_spread
        )
        
        # 生成建议
        recommendation = self._generate_recommendation(score, strategy, market)
        
        return ProductOpportunity(
            sku=product['sku'],
            title=product['title'],
            dajian_cost=round(total_cost, 2),
            market_avg_price=market.avg_price,
            potential_margin=round(potential_margin, 2),
            margin_rate=round(margin_rate, 3),
            competition_level=market.competition_level,
            opportunity_score=score,
            recommendation=recommendation,
            smart_price=smart_price,
            strategy=strategy,
            matched_keywords=[keyword]
        )
    
    def _calculate_opportunity_score(self, margin_rate: float, 
                                    competition_level: str, 
                                    price_spread: float,
                                    real_str: float = None,
                                    estimated_str: float = None) -> int:
        """统一委托到 scoring.opportunity_score()。保留旧签名以兼容旧调用点。"""
        from .scoring import opportunity_score, ScoringInputs
        return opportunity_score(ScoringInputs(
            margin_rate=margin_rate,
            competition_level=competition_level,  # type: ignore[arg-type]
            price_spread=price_spread,
            real_str=real_str,
            estimated_str=estimated_str,
        ))
    
    def _generate_recommendation(self, score: int, strategy: str, 
                                market: MarketAnalysis) -> str:
        """生成推荐建议"""
        if score >= 80:
            return f"🔥 强烈推荐 - {strategy} | 竞争{market.competition_level}"
        elif score >= 60:
            return f"✅ 推荐刊登 - {strategy}"
        elif score >= 40:
            return f"⚠️ 谨慎考虑 - 利润率偏低"
        else:
            return f"❌ 不推荐 - 市场竞争激烈或利润不足"
    
    # ========== 3. Smart Pricing (智能定价) ==========

    def calculate_smart_price(self, total_cost: float, market_price: float,
                             min_margin: float = 0.10,
                             max_margin: float = 0.35) -> Dict:
        """
        智能定价 — 委托到 PricingEngine.calculate_smart_price (单一真相源).

        ⚠️ 历史问题修复 (2026-05): 旧实现用 cost*(1+margin) 计算底价/利润率,
        完全忽略 13.25% eBay FVF + 5% 广告 + 5% 店铺折扣 + $0.30 固定费,
        导致 MI / Tab 7 给的"建议价"系统性低估约 28%, 照建议价改价直接亏本.
        现已统一委托到 PricingEngine, 与 batch_smart_reprice / app.py / batch_publish 共用同一公式与死线.

        Args:
            total_cost: 总到岸成本 (PricingEngine.calculate_dajian_cost.total_dajian_cost)
            market_price: 市场均价
            min_margin: 最低净利润率 (相对净收, 默认 10%)
            max_margin: 最高净利润率 (相对净收, 默认 35%)

        Returns:
            {final_price, floor_price, ceiling_price, competitive_price, strategy, margin, ...}
            其中 margin 是基于卖家净收 (扣完所有费率与折扣) 的真实利润率, 非毛利率.
        """
        from src.services.pricing_engine import PricingEngine
        result = PricingEngine.calculate_smart_price(
            total_cost=total_cost,
            market_price=market_price,
            min_margin=min_margin,
            max_margin=max_margin,
        )
        # 兼容旧调用点: 旧返回不含 market_price/min_margin/max_margin/status,
        # PricingEngine 都给, 多出来的字段无害.
        return result

    
    # ========== 4. Competitor Pain Points (竞品痛点分析) ==========
    
    def analyze_competitor_pain_points(self, keyword: str) -> List[CompetitorPainPoint]:
        """
        分析竞品痛点 (基于常见家具类问题)
        
        注意: 真正的痛点分析需要爬取评论数据
        这里提供常见痛点模板，后续可扩展 API
        """
        # 常见家具类痛点数据库
        furniture_pain_points = {
            'sofa': [
                CompetitorPainPoint(
                    issue="腿部不稳固",
                    frequency=85,
                    our_advantage="加固实木腿设计",
                    suggested_emphasis="Reinforced solid wood legs for stability"
                ),
                CompetitorPainPoint(
                    issue="组装困难",
                    frequency=72,
                    our_advantage="简易组装设计",
                    suggested_emphasis="Easy 15-minute assembly with clear instructions"
                ),
                CompetitorPainPoint(
                    issue="坐垫变形",
                    frequency=65,
                    our_advantage="高密度海绵",
                    suggested_emphasis="High-density foam cushions retain shape"
                )
            ],
            'table': [
                CompetitorPainPoint(
                    issue="表面易刮花",
                    frequency=78,
                    our_advantage="耐磨涂层",
                    suggested_emphasis="Scratch-resistant surface coating"
                ),
                CompetitorPainPoint(
                    issue="桌腿晃动",
                    frequency=68,
                    our_advantage="可调节脚垫",
                    suggested_emphasis="Adjustable leveling feet for stability"
                )
            ],
            'chair': [
                CompetitorPainPoint(
                    issue="久坐不舒适",
                    frequency=82,
                    our_advantage="人体工学设计",
                    suggested_emphasis="Ergonomic design for all-day comfort"
                ),
                CompetitorPainPoint(
                    issue="轮子质量差",
                    frequency=60,
                    our_advantage="静音耐磨滚轮",
                    suggested_emphasis="Smooth-rolling, floor-safe casters"
                )
            ],
            'cabinet': [
                CompetitorPainPoint(
                    issue="抽屉卡顿",
                    frequency=75,
                    our_advantage="金属滑轨",
                    suggested_emphasis="Premium metal drawer slides"
                ),
                CompetitorPainPoint(
                    issue="板材气味",
                    frequency=55,
                    our_advantage="环保材料",
                    suggested_emphasis="CARB-certified, low-emission materials"
                )
            ]
        }
        
        # 匹配关键词
        keyword_lower = keyword.lower()
        
        for category, pain_points in furniture_pain_points.items():
            if category in keyword_lower:
                return pain_points
        
        # 默认通用痛点
        return [
            CompetitorPainPoint(
                issue="质量问题",
                frequency=70,
                our_advantage="严格质检",
                suggested_emphasis="Quality-checked before shipping"
            ),
            CompetitorPainPoint(
                issue="配送损坏",
                frequency=50,
                our_advantage="专业包装",
                suggested_emphasis="Securely packaged for safe delivery"
            )
        ]
    
    # ========== 5. Hot Keywords Discovery (热门关键词发现) ==========
    
    def discover_hot_keywords(self, category_id: str = None, 
                             limit: int = 10) -> List[Dict]:
        """
        发现热门关键词
        
        基于 Browse API 分析热门产品提取关键词
        """
        # 家具类热门品类
        furniture_categories = [
            ("sofa", "沙发"),
            ("coffee table", "茶几"),
            ("dining table", "餐桌"),
            ("office chair", "办公椅"),
            ("bookshelf", "书架"),
            ("tv stand", "电视柜"),
            ("bed frame", "床架"),
            ("dresser", "梳妆台"),
            ("nightstand", "床头柜"),
            ("storage cabinet", "储物柜")
        ]
        
        hot_keywords = []
        
        for kw_en, kw_cn in furniture_categories[:limit]:
            market = self.analyze_market(kw_en, category_id)

            # 没有市场数据的关键词不应进入热门榜
            if market.avg_price <= 0:
                continue

            from .scoring import opportunity_score, ScoringInputs
            # 这里没有 cost/margin 上下文，给一个保守的默认 margin (0.20)
            # 用来在统一评分体系下排序"竞争 vs 价格分散度"
            score = opportunity_score(ScoringInputs(
                margin_rate=0.20,
                competition_level=market.competition_level,  # type: ignore[arg-type]
                price_spread=market.price_spread,
            ))

            hot_keywords.append({
                "keyword": kw_en,
                "keyword_cn": kw_cn,
                "avg_price": market.avg_price,
                "competition": market.competition_level,
                "active_total": market.active_total,
                "listings": market.total_listings,
                "score": score,
            })
        
        # 按分数排序
        hot_keywords.sort(key=lambda x: x['score'], reverse=True)
        
        return hot_keywords
    
    # ========== 6. Auto Discovery (自动爆品发掘) ==========
    
    def auto_discover_opportunities(self, min_margin: float = 0.20,
                                   max_results: int = 20) -> List[Dict]:
        """
        自动发掘爆品机会 - 核心功能
        
        工作流程:
        1. 扫描所有大建库存产品
        2. 从产品标题提取关键词
        3. 查询 eBay 市场数据获取真实售价
        4. 计算利润空间和机会分数
        5. 返回按潜力排序的推荐列表
        
        Args:
            min_margin: 最低利润率要求 (默认 20%)
            max_results: 最大返回数量
        
        Returns:
            按机会分数排序的产品列表
        """
        products = self._get_local_inventory()
        
        if not products:
            return []
        
        # F1 — 应用 SKU 屏蔽名单（与 Streamlit MI 页面共用 reports/mi_blacklist.json）
        blacklist = self._load_mi_blacklist()
        
        # 去重
        unique_products = []
        seen_skus = set()
        for product in products:
            sku = product.get('sku', '')
            if not sku or sku in seen_skus:
                continue
            if sku in blacklist:
                continue
            title = product.get('title', '')
            total_cost = product.get('total_cost', 0)
            if not title or total_cost <= 0:
                continue
            seen_skus.add(sku)
            unique_products.append(product)

        # 并发评估 - 受 TERAPEAK_CONCURRENCY 控制 (默认 6)
        from concurrent.futures import ThreadPoolExecutor

        try:
            workers = max(1, min(16, int(os.getenv("TERAPEAK_CONCURRENCY", "6"))))
        except ValueError:
            workers = 6

        # F6 — pull seller-measured performance once (cached read, no API call).
        # When a candidate SKU has ≥ 50 impressions we feed real_str into the
        # opportunity score so the STR weight jumps from estimated (max 18)
        # to real (max 30). Stale / missing cache => empty dict, fall back
        # to the Browse-only demand signal.
        from .performance_bridge import load_seller_performance_index
        seller_perf = load_seller_performance_index(max_age_hours=24)

        def _evaluate(product: Dict) -> Optional[Dict]:
            sku = product['sku']
            title = product['title']
            total_cost = product['total_cost']
            search_keywords = self._extract_search_keywords(title)
            if not search_keywords:
                return None
            try:
                market = self.analyze_market(search_keywords)
            except Exception:
                return None
            if market.avg_price <= 0:
                return None

            smart_result = self.calculate_smart_price(
                total_cost=total_cost,
                market_price=market.avg_price,
                min_margin=min_margin,
                max_margin=0.40,
            )
            smart_price = smart_result['final_price']
            # 净利润 (相对净收, 已扣 eBay 费 + 广告 + 折扣 + 固定费):
            #   net_revenue = listing_price × discount_denom - fixed_fee
            #   profit      = net_revenue - cost
            # PricingEngine.calculate_smart_price 已返回 margin (净利润率).
            margin_rate = float(smart_result.get('margin', 0))  # 已是净利率, 0~1
            potential_profit = total_cost * margin_rate  # 折算到美元绝对值
            if margin_rate < min_margin:
                return None

            # F6 — join with seller performance bridge
            perf = seller_perf.get(sku) or {}
            seller_str = perf.get('str_pct')  # None when impressions < 50
            # When we have no real seller STR (the common case — 大多数 SKU 曝光
            # < 50), fall back to the same Browse-only estimated STR that
            # analyze_market already derives. Without this the STR component of
            # the opportunity score is permanently 0, capping furniture scores
            # (competition 恒 very_high → 0 分) below the 推荐 threshold and
            # producing days of all-"暂不推荐" snapshots.
            est_str = None
            if seller_str is None:
                try:
                    from .trend_discovery import TrendDiscovery
                    est_str = TrendDiscovery.get_estimated_str(search_keywords)
                except Exception:
                    est_str = None
            score = self._calculate_opportunity_score(
                margin_rate=margin_rate,
                competition_level=market.competition_level,
                price_spread=market.price_spread,
                real_str=seller_str,
                estimated_str=est_str,
            )
            price_advantage = "低于市场均价" if smart_price < market.avg_price else "接近市场均价"
            return {
                'sku': sku,
                'title': title,
                'search_keywords': search_keywords,
                'dajian_cost': round(total_cost, 2),
                'market_avg_price': market.avg_price,
                'market_median_price': market.median_price,
                'suggested_price': round(smart_price, 2),
                'potential_profit': round(potential_profit, 2),
                'margin_rate': round(margin_rate * 100, 1),
                'competition': market.competition_level,
                'active_total': market.active_total,
                'opportunity_score': score,
                'price_advantage': price_advantage,
                'demand_signal_score': market.demand_signal_score,
                'demand_signal_label': market.demand_signal_label,
                # F6 — surface real seller signals so MI cards/table can show them
                'seller_str_pct': seller_str,
                'seller_impressions': perf.get('impressions', 0),
                'seller_sold': perf.get('sold_qty', 0),
                'seller_has_sales': bool(perf.get('has_sales', False)),
                'seller_listing_id': perf.get('listing_id', '') or product.get('listing_id', ''),
                'strategy': smart_result['strategy'],
                'recommendation': self._get_discovery_recommendation(score, margin_rate, market),
                'url': product.get('url', ''),
                'status': product.get('status', ''),
                'image_url': product.get('image_url', ''),
                'images': product.get('images', []),
            }

        opportunities: List[Dict] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for item in ex.map(_evaluate, unique_products):
                if item is not None:
                    opportunities.append(item)

        # 按机会分数排序
        opportunities.sort(key=lambda x: x['opportunity_score'], reverse=True)
        
        return opportunities[:max_results]
    
    def _extract_search_keywords(self, title: str) -> str:
        """
        从产品标题提取搜索关键词
        
        策略:
        - 清理标题中的噪音（尺寸、[Video]、品牌前缀等）
        - 识别核心产品词 (sofa, chair, table 等)
        - 保留重要修饰词 (sectional, modular, leather 等)
        """
        import re
        
        # 预处理：清理标题
        cleaned_title = title
        
        # 移除 [Video], [xxx] 等标记
        cleaned_title = re.sub(r'\[.*?\]', '', cleaned_title)
        
        # 移除尺寸信息 (如 "87.4", "59.84\"")
        cleaned_title = re.sub(r'\d+\.?\d*[\"\']?\s*[-x×]?\s*', '', cleaned_title)
        
        # 移除常见的分隔符和噪音
        cleaned_title = re.sub(r'\s*[-–—]\s*', ' ', cleaned_title)
        cleaned_title = re.sub(r'\s+', ' ', cleaned_title).strip()
        
        title_lower = cleaned_title.lower()
        
        # 核心产品词映射 (按长度排序，优先匹配长词)
        product_keywords = {
            'sectional sofa': 'sectional sofa',
            'modular sofa': 'modular sofa',
            'sleeper sofa': 'sleeper sofa',
            'recliner sofa': 'reclining sofa',
            'gaming chair': 'gaming chair',
            'office chair': 'office chair',
            'dining chair': 'dining chair',
            'accent chair': 'accent chair',
            'recliner chair': 'recliner',
            'coffee table': 'coffee table',
            'dining table': 'dining table',
            'side table': 'side table',
            'end table': 'end table',
            'console table': 'console table',
            'kitchen island': 'kitchen island',
            'bed frame': 'bed frame',
            'platform bed': 'platform bed',
            'bunk bed': 'bunk bed',
            'tv stand': 'tv stand',
            'entertainment center': 'entertainment center',
            'storage cabinet': 'storage cabinet',
            'wine cabinet': 'wine cabinet',
            'file cabinet': 'file cabinet',
            'wing chair': 'wing chair',
            'upholstered chair': 'upholstered chair',
            'sofa': 'sofa',
            'couch': 'sofa',
            'loveseat': 'loveseat',
            'futon': 'futon',
            'daybed': 'daybed',
            'chair': 'chair',
            'recliner': 'recliner',
            'table': 'table',
            'desk': 'desk',
            'bed': 'bed',
            'dresser': 'dresser',
            'nightstand': 'nightstand',
            'bookshelf': 'bookshelf',
            'bookcase': 'bookcase',
            'cabinet': 'cabinet',
            'wardrobe': 'wardrobe',
            'ottoman': 'ottoman',
            'bench': 'bench',
            'stool': 'bar stool',
            'vanity': 'vanity',
            'mirror': 'mirror',
            'island': 'kitchen island',
        }
        
        # 重要修饰词
        modifiers = [
            'modular', 'convertible', 'l-shaped', 'u-shaped', 
            'sectional', 'reclining', 'sleeper', 'storage',
            'leather', 'fabric', 'velvet', 'linen', 'faux leather', 'corduroy',
            'modern', 'mid-century', 'industrial', 'farmhouse', 'vintage',
            'adjustable', 'folding', 'extendable', 'upholstered',
            'wood', 'metal', 'glass', 'marble'
        ]
        
        # 找到核心产品词 (按词长度降序匹配)
        core_keyword = None
        for phrase, search_term in sorted(product_keywords.items(), key=lambda x: -len(x[0])):
            if phrase in title_lower:
                core_keyword = search_term
                break
        
        if not core_keyword:
            # 如果没有匹配到，尝试提取有意义的词
            # 过滤掉品牌名、颜色等
            skip_words = {'topmax', 'maxyoyo', 'zafly', 'k&k', 'a&a', 
                         'furniture', 'piece', 'set', 'with', 'and', 'for',
                         'white', 'black', 'gray', 'grey', 'brown', 'beige',
                         'blue', 'green', 'red', 'pink', 'yellow', 'orange',
                         'foam', 'wood', 'solid', 'mdf', 'plastic', 'rattan'}
            
            words = [w for w in cleaned_title.split() 
                    if w.lower() not in skip_words and len(w) > 2]
            
            if words:
                # 取前2-3个有意义的词
                return ' '.join(words[:3])
            else:
                return "furniture"  # 默认返回家具
        
        # 添加重要修饰词
        found_modifiers = []
        for mod in modifiers:
            if mod in title_lower and mod not in core_keyword:
                found_modifiers.append(mod)
                if len(found_modifiers) >= 1:  # 最多1个修饰词
                    break
        
        if found_modifiers:
            return f"{' '.join(found_modifiers)} {core_keyword}"
        
        return core_keyword
    
    def _get_discovery_recommendation(self, score: int, margin_rate: float, 
                                     market: MarketAnalysis) -> str:
        """生成发掘推荐"""
        margin_pct = margin_rate * 100
        
        if score >= 80:
            return f"🔥 强烈推荐刊登 - 利润率{margin_pct:.0f}%, 竞争{market.competition_level}"
        elif score >= 65:
            return f"✅ 推荐刊登 - 利润率{margin_pct:.0f}%"
        elif score >= 50:
            return f"⚠️ 可以考虑 - 利润率{margin_pct:.0f}%, 需差异化运营"
        else:
            return f"❌ 暂不推荐 - 利润空间或市场竞争需优化"
    
    # ========== 7. Full Intelligence Report (完整智能报告) ==========
    
    def generate_intelligence_report(self, keywords: List[str] = None) -> Dict:
        """
        生成完整的市场智能报告
        
        Returns:
            包含市场分析、推荐产品、定价建议的完整报告
        """
        if not keywords:
            # 使用默认热门关键词
            hot = self.discover_hot_keywords(limit=5)
            keywords = [h['keyword'] for h in hot]
        
        report = {
            "generated_at": datetime.now().isoformat(),
            "keywords_analyzed": keywords,
            "market_analysis": {},
            "recommended_products": [],
            "pain_points": {},
            "summary": {}
        }
        
        # 1. 分析每个关键词的市场
        for kw in keywords:
            market = self.analyze_market(kw)
            report["market_analysis"][kw] = market.to_dict()
            
            # 收集痛点
            pain_points = self.analyze_competitor_pain_points(kw)
            report["pain_points"][kw] = [asdict(p) for p in pain_points]
        
        # 2. 反向选品
        opportunities = self.reverse_source(keywords, min_margin=0.15, max_results=10)
        report["recommended_products"] = [opp.to_dict() for opp in opportunities]
        
        # 3. 生成摘要
        high_potential = len([o for o in opportunities if o.opportunity_score >= 70])
        avg_margin = sum(o.margin_rate for o in opportunities) / len(opportunities) if opportunities else 0
        
        report["summary"] = {
            "total_keywords": len(keywords),
            "total_opportunities": len(opportunities),
            "high_potential_count": high_potential,
            "average_margin_rate": round(avg_margin, 3),
            "recommendation": self._generate_summary_recommendation(opportunities)
        }
        
        return report
    
    def _generate_summary_recommendation(self, opportunities: List[ProductOpportunity]) -> str:
        """生成摘要建议"""
        if not opportunities:
            return "未发现匹配的库存产品，建议扩大采集范围"
        
        high_score = [o for o in opportunities if o.opportunity_score >= 70]
        
        if len(high_score) >= 3:
            return f"🔥 发现 {len(high_score)} 个高潜力产品，建议优先刊登"
        elif len(high_score) >= 1:
            return f"✅ 发现 {len(high_score)} 个推荐产品，可考虑刊登"
        else:
            return "⚠️ 当前库存产品竞争力一般，建议调整采集策略"
