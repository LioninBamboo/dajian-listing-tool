"""
每日 Terapeak 市场调研报告

功能:
1. 扫描家具类热门品类，分析 sell-through rate
2. 发现高潜力关键词和产品机会
3. 将我们库存与市场数据对比，找出最佳刊登候选
4. 生成 HTML 报告并保存 / 可选邮件发送

用法:
  python daily_terapeak_report.py              # 生成报告并保存到 reports/
  python daily_terapeak_report.py --email      # 生成报告并发送邮件
  
可在 daily_tasks.py 或 scheduled task 中调用
"""
import os
import sys
import io

# Ensure UTF-8 output (avoid emoji crashes on Chinese Windows scheduled tasks)
if sys.stdout and hasattr(sys.stdout, 'encoding') and sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import Counter

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")


# ========== 配置 ==========
# 要研究的家具品类关键词（按重要性排序）
RESEARCH_CATEGORIES = [
    # (搜索关键词, 中文名, eBay 品类 ID, 最低价格过滤)
    ("sectional sofa", "组合沙发", "38208", 200),
    ("l shaped sofa", "L形沙发", "38208", 150),
    ("modular sofa", "模块化沙发", "38208", 200),
    ("sleeper sofa", "沙发床", "38208", 150),
    ("reclining sofa", "可躺沙发", "38208", 200),
    ("convertible sofa", "可转换沙发", "38208", 100),
    ("coffee table", "茶几", "38204", 50),
    ("dining table set", "餐桌套装", "38204", 100),
    ("tv stand", "电视柜", "38040", 50),
    ("office chair", "办公椅", "20081", 50),
    ("gaming chair", "电竞椅", "20081", 80),
    ("dresser", "梳妆台", "38219", 80),
    ("bookshelf", "书架", "20487", 30),
    ("storage cabinet", "储物柜", "38221", 40),
    ("bed frame", "床架", "175758", 80),
    ("kitchen island", "厨房岛台", "183319", 80),
    ("bathroom vanity", "浴室柜", "176992", 80),
    ("outdoor furniture", "户外家具", "177031", 50),
    ("nightstand", "床头柜", "38219", 20),
    ("accent chair", "装饰椅", "38208", 50),
    ("bar stool", "吧台凳", "38205", 30),
    ("shoe rack", "鞋架", "38221", 15),
    ("dog crate", "狗笼", "46289", 30),
    ("cat tree", "猫爬架", "46289", 20),
]

# 报告输出目录
REPORT_DIR = PROJECT_ROOT / "reports"

# 邮件配置 - 复用已有的 NOTIFICATION_EMAIL 环境变量
EMAIL_CONFIG = {
    "sender_email": os.getenv("NOTIFICATION_EMAIL", "").strip(),
    "sender_password": os.getenv("NOTIFICATION_EMAIL_PASSWORD", "").strip(),
    "recipient_email": os.getenv("NOTIFICATION_EMAIL", "").strip(),
}


class DailyTerapeakReport:
    """每日 Terapeak 市场调研报告生成器"""
    
    def __init__(self):
        from src.plugins.terapeak_research.research_client import TerapeakClient
        self.terapeak = TerapeakClient()
        self.db_path = str(PROJECT_ROOT / "ebay_collection.db")
        self.report_data = {
            "generated_at": datetime.now().isoformat(),
            "categories": [],
            "top_opportunities": [],
            "inventory_matches": [],
            "summary": {}
        }
    
    def _get_local_inventory(self) -> List[Dict]:
        """获取本地库存产品"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        cur.execute("""
            SELECT sku, title, url, cost_breakdown, status, suggested_price
            FROM collected_products 
            WHERE status IN ('PENDING', 'READY', 'COLLECTED')
              AND cost_breakdown IS NOT NULL
        """)
        
        products = []
        for row in cur.fetchall():
            cost_data = json.loads(row['cost_breakdown']) if row['cost_breakdown'] else {}
            products.append({
                'sku': row['sku'],
                'title': row['title'],
                'url': row['url'],
                'status': row['status'],
                'suggested_price': row['suggested_price'] or 0,
                'total_cost': cost_data.get('total_dajian_cost', 0)
            })
        
        conn.close()
        return products
    
    def research_category(self, keyword: str, category_id: str = None, 
                         min_price: float = 50) -> Dict:
        """
        研究单个品类的市场数据
        
        Returns:
            {
                'keyword': str,
                'total_listings': int,
                'avg_price': float,
                'min_price': float,
                'max_price': float,
                'median_price': float,
                'price_spread': float,
                'competition_level': str,
                'top_titles': list,
                'common_aspects': dict,
                'sell_through_indicators': dict
            }
        """
        print(f"  📊 Researching: {keyword}...")
        
        # 搜索 Best Match（最相关 = 最畅销的排在前面）
        best_match_items = self.terapeak.search_products(
            keyword, category_id, limit=50, sort="bestMatch",
            min_price=min_price, buy_it_now_only=True
        )
        
        if not best_match_items:
            return {'keyword': keyword, 'total_listings': 0, 'error': 'No items found'}
        
        # 价格分析
        prices = [item['price'] for item in best_match_items if item.get('price', 0) > 0]
        
        if not prices:
            return {'keyword': keyword, 'total_listings': len(best_match_items), 'error': 'No valid prices'}
        
        # IQR 过滤异常值
        sorted_prices = sorted(prices)
        q1 = sorted_prices[len(sorted_prices) // 4]
        q3 = sorted_prices[3 * len(sorted_prices) // 4]
        iqr = q3 - q1
        filtered = [p for p in prices if (q1 - 1.5 * iqr) <= p <= (q3 + 1.5 * iqr)]
        if len(filtered) < 5:
            filtered = prices
        
        avg_price = sum(filtered) / len(filtered)
        median_price = sorted(filtered)[len(filtered) // 2]
        std_dev = (sum((p - avg_price) ** 2 for p in filtered) / len(filtered)) ** 0.5
        price_spread = std_dev / avg_price if avg_price > 0 else 0
        
        # Sell-through indicators (估算)
        # eBay Best Match 排序本身就反映了 sell-through
        # 前面的商品 = 卖得更好
        top_10 = best_match_items[:10]
        top_10_prices = [i['price'] for i in top_10 if i.get('price', 0) > 0]
        top_10_avg = sum(top_10_prices) / len(top_10_prices) if top_10_prices else 0
        
        # 竞争程度
        total = len(best_match_items)
        if total < 20:
            comp = "Low"
        elif total < 40:
            comp = "Medium"
        else:
            comp = "High"
        
        # 提取 top-selling 标题中的高频词
        all_words = []
        for item in best_match_items[:20]:
            title = item.get('title', '').lower()
            # 清理
            title = title.replace(',', ' ').replace('-', ' ').replace('&', ' ')
            words = [w.strip() for w in title.split() if len(w.strip()) > 2]
            skip = {'the', 'and', 'for', 'with', 'new', 'set', 'hot', 'sale', 'free', 'ship', 'shipping'}
            all_words.extend([w for w in words if w not in skip])
        
        word_freq = Counter(all_words).most_common(20)
        
        return {
            'keyword': keyword,
            'total_listings': total,
            'avg_price': round(avg_price, 2),
            'min_price': round(min(filtered), 2),
            'max_price': round(max(filtered), 2),
            'median_price': round(median_price, 2),
            'price_spread': round(price_spread, 3),
            'competition_level': comp,
            'top_10_avg_price': round(top_10_avg, 2),
            'top_titles': [item.get('title', '') for item in best_match_items[:5]],
            'top_keywords': word_freq,
            'sell_through_indicators': {
                'best_match_top10_avg': round(top_10_avg, 2),
                'price_premium_vs_avg': round(top_10_avg - avg_price, 2) if top_10_avg and avg_price else 0,
                'estimated_demand': 'High' if total >= 40 else ('Medium' if total >= 20 else 'Low')
            }
        }
    
    def match_inventory(self, category_data: Dict, inventory: List[Dict]) -> List[Dict]:
        """将品类数据与库存匹配"""
        keyword = category_data.get('keyword', '').lower()
        kw_parts = keyword.split()
        
        matches = []
        for product in inventory:
            title_lower = product['title'].lower()
            # 至少 50% 关键词匹配
            matched = sum(1 for w in kw_parts if w in title_lower)
            if matched >= len(kw_parts) * 0.5:
                cost = product.get('total_cost', 0)
                market_avg = category_data.get('avg_price', 0)
                
                # 精确利润计算：扣除 eBay 费用后的净利润
                # eBay 费率: 13.25% 佣金 + 5% 广告 + $0.30 固定费
                ebay_fee_rate = 0.1325 + 0.05  # 18.25%
                ebay_fixed_fee = 0.30
                net_revenue = market_avg * (1 - ebay_fee_rate) - ebay_fixed_fee
                net_profit = net_revenue - cost
                net_margin = net_profit / cost if cost > 0 else 0
                
                matches.append({
                    'sku': product['sku'],
                    'title': product['title'],
                    'status': product['status'],
                    'cost': round(cost, 2),
                    'market_avg': market_avg,
                    'ebay_fees': round(market_avg * ebay_fee_rate + ebay_fixed_fee, 2),
                    'net_revenue': round(net_revenue, 2),
                    'net_profit': round(net_profit, 2),
                    'potential_margin': round(net_margin * 100, 1),
                    'opportunity': '🔥' if net_margin >= 0.25 else ('✅' if net_margin >= 0.15 else ('⚠️' if net_margin >= 0 else '❌'))
                })
        
        return matches
    
    def generate_report(self) -> Dict:
        """
        生成完整的每日调研报告
        """
        print("=" * 60)
        print(f"📊 每日 Terapeak 市场调研报告")
        print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print("=" * 60)
        
        inventory = self._get_local_inventory()
        print(f"\n📦 本地库存: {len(inventory)} 个产品")
        
        all_categories = []
        all_inventory_matches = []
        
        for kw, kw_cn, cat_id, min_price in RESEARCH_CATEGORIES:
            try:
                data = self.research_category(kw, cat_id, min_price)
                data['keyword_cn'] = kw_cn
                data['category_id'] = cat_id
                all_categories.append(data)
                
                # 匹配库存
                if data.get('total_listings', 0) > 0:
                    matches = self.match_inventory(data, inventory)
                    for m in matches:
                        m['market_keyword'] = kw
                        m['market_keyword_cn'] = kw_cn
                    all_inventory_matches.extend(matches)
                
                if data.get('total_listings', 0) > 0:
                    print(f"     ✅ {kw} ({kw_cn}): {data['total_listings']} listings, avg ${data['avg_price']}, competition: {data['competition_level']}")
                else:
                    print(f"     ⚠️ {kw} ({kw_cn}): No data")
                    
            except Exception as e:
                print(f"     ❌ {kw} ({kw_cn}): Error - {e}")
                all_categories.append({
                    'keyword': kw, 'keyword_cn': kw_cn, 
                    'total_listings': 0, 'error': str(e)
                })
        
        # 按 sell-through 指标排序品类
        valid_cats = [c for c in all_categories if c.get('total_listings', 0) > 0]
        valid_cats.sort(key=lambda c: c.get('sell_through_indicators', {}).get('best_match_top10_avg', 0), reverse=True)
        
        # 去重库存匹配
        seen_skus = set()
        unique_matches = []
        for m in sorted(all_inventory_matches, key=lambda x: x['potential_margin'], reverse=True):
            if m['sku'] not in seen_skus:
                seen_skus.add(m['sku'])
                unique_matches.append(m)
        
        # ===== 品类缺口分析 =====
        # 找出有市场需求但库存中没有匹配产品的品类
        matched_keywords = set(m.get('market_keyword', '') for m in unique_matches)
        category_gaps = []
        for cat in valid_cats:
            kw = cat.get('keyword', '')
            match_count = sum(1 for m in unique_matches if m.get('market_keyword') == kw)
            if match_count == 0 and cat.get('avg_price', 0) > 0:
                category_gaps.append({
                    'keyword': kw,
                    'keyword_cn': cat.get('keyword_cn', ''),
                    'avg_price': cat.get('avg_price', 0),
                    'top_10_avg_price': cat.get('top_10_avg_price', 0),
                    'competition_level': cat.get('competition_level', ''),
                    'total_listings': cat.get('total_listings', 0),
                    'top_titles': cat.get('top_titles', [])[:3],
                    'reason': '库存无匹配产品'
                })
            elif match_count <= 2 and cat.get('sell_through_indicators', {}).get('estimated_demand') == 'High':
                category_gaps.append({
                    'keyword': kw,
                    'keyword_cn': cat.get('keyword_cn', ''),
                    'avg_price': cat.get('avg_price', 0),
                    'top_10_avg_price': cat.get('top_10_avg_price', 0),
                    'competition_level': cat.get('competition_level', ''),
                    'total_listings': cat.get('total_listings', 0),
                    'match_count': match_count,
                    'top_titles': cat.get('top_titles', [])[:3],
                    'reason': f'高需求但仅匹配 {match_count} 个产品'
                })
        
        # ===== 未刊登高潜力产品 =====
        unpublished_priority = [
            m for m in unique_matches 
            if m.get('status') != 'PUBLISHED' and m.get('potential_margin', 0) >= 15
        ]
        
        self.report_data = {
            "generated_at": datetime.now().isoformat(),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "categories": all_categories,
            "valid_categories": len(valid_cats),
            "top_opportunities": valid_cats[:10],
            "inventory_matches": unique_matches[:40],
            "category_gaps": category_gaps,
            "unpublished_priority": unpublished_priority[:20],
            "inventory_total": len(inventory),
            "summary": {
                "total_categories_researched": len(RESEARCH_CATEGORIES),
                "categories_with_data": len(valid_cats),
                "high_demand_categories": len([c for c in valid_cats if c.get('sell_through_indicators', {}).get('estimated_demand') == 'High']),
                "inventory_products_with_opportunity": len(unique_matches),
                "high_margin_products": len([m for m in unique_matches if m['potential_margin'] >= 25]),
                "category_gaps_count": len(category_gaps),
                "unpublished_high_potential": len(unpublished_priority),
            }
        }

        # F4 — 携带市场智能机会快照 (auto_discover_opportunities 输出)
        self.report_data["mi_opportunities"] = self._load_latest_mi_snapshot(max_age_hours=24)
        
        print(f"\n📊 报告摘要:")
        print(f"   研究品类: {len(RESEARCH_CATEGORIES)}")
        print(f"   有数据品类: {len(valid_cats)}")
        print(f"   高需求品类: {self.report_data['summary']['high_demand_categories']}")
        print(f"   库存匹配机会: {len(unique_matches)}")
        print(f"   高利润产品 (>25%): {self.report_data['summary']['high_margin_products']}")
        
        return self.report_data
    
    def _load_latest_mi_snapshot(self, max_age_hours: int = 24) -> dict:
        """F4 — 加载最近一次 Streamlit 市场智能页面产生的机会快照。

        避免在报表中重复调用 eBay API；没有快照时返回空结构。
        """
        try:
            from datetime import datetime as _dt, timedelta as _td
            REPORT_DIR.mkdir(exist_ok=True)
            snaps = sorted(REPORT_DIR.glob("mi_opportunities_*.json"), reverse=True)
            if not snaps:
                return {"available": False, "reason": "no snapshot", "opportunities": []}
            latest = snaps[0]
            try:
                payload = json.loads(latest.read_text(encoding="utf-8"))
            except Exception as e:
                return {"available": False, "reason": f"snapshot parse error: {e}", "opportunities": []}

            generated_at = payload.get("generated_at")
            try:
                age_h = (_dt.now() - _dt.fromisoformat(generated_at)).total_seconds() / 3600 if generated_at else None
            except Exception:
                age_h = None

            if age_h is not None and age_h > max_age_hours:
                return {
                    "available": False,
                    "reason": f"snapshot stale ({age_h:.1f}h > {max_age_hours}h)",
                    "snapshot_file": latest.name,
                    "opportunities": [],
                }

            opps = payload.get("opportunities", []) or []
            return {
                "available": True,
                "snapshot_file": latest.name,
                "generated_at": generated_at,
                "age_hours": round(age_h, 1) if age_h is not None else None,
                "min_margin": payload.get("min_margin"),
                "opportunities": opps[:15],  # 只取前 15 个进报表
                "total_in_snapshot": len(opps),
            }
        except Exception as e:
            return {"available": False, "reason": f"loader error: {e}", "opportunities": []}
    
    def generate_html_report(self) -> str:
        """生成专业级 HTML 格式报告"""
        data = self.report_data
        date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
        summary = data.get("summary", {})
        inventory_total = data.get("inventory_total", 0)
        
        # 计算额外的智能指标
        valid_cats = [c for c in data.get('categories', []) if c.get('total_listings', 0) > 0]
        avg_market_price = sum(c.get('avg_price', 0) for c in valid_cats) / len(valid_cats) if valid_cats else 0
        high_margin_matches = [m for m in data.get('inventory_matches', []) if m.get('potential_margin', 0) >= 25]
        unpublished_opportunities = [m for m in high_margin_matches if m.get('status') != 'PUBLISHED']
        
        # 按利润率分组
        margin_groups = {'🔥 >50%': 0, '✅ 25-50%': 0, '⚠️ 15-25%': 0, '❌ <15%': 0}
        for m in data.get('inventory_matches', []):
            mg = m.get('potential_margin', 0)
            if mg >= 50: margin_groups['🔥 >50%'] += 1
            elif mg >= 25: margin_groups['✅ 25-50%'] += 1
            elif mg >= 15: margin_groups['⚠️ 15-25%'] += 1
            else: margin_groups['❌ <15%'] += 1

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AquaVerve 市场情报 - {date_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 960px; margin: 0 auto; padding: 16px; background: #f0f2f5; color: #1a1a2e; font-size: 14px; line-height: 1.6; }}
  
  /* Header */
  .header {{ background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%); color: white; padding: 28px 24px; border-radius: 12px; margin-bottom: 16px; position: relative; overflow: hidden; }}
  .header::after {{ content: ''; position: absolute; top: 0; right: 0; width: 200px; height: 100%; background: radial-gradient(circle at 80% 50%, rgba(212,175,55,0.15) 0%, transparent 70%); }}
  .header h1 {{ font-size: 20px; color: #d4af37; letter-spacing: 3px; font-weight: 600; }}
  .header .subtitle {{ color: #b8b8d1; font-size: 12px; margin-top: 6px; letter-spacing: 1px; }}
  .header .date-badge {{ display: inline-block; background: rgba(212,175,55,0.2); color: #d4af37; padding: 4px 12px; border-radius: 20px; font-size: 12px; margin-top: 10px; border: 1px solid rgba(212,175,55,0.3); }}
  
  /* Executive Summary */
  .exec-summary {{ background: white; border-radius: 10px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-left: 4px solid #d4af37; }}
  .exec-summary h2 {{ font-size: 15px; color: #302b63; margin-bottom: 12px; }}
  .exec-summary .insight {{ background: #f8f7ff; padding: 12px 16px; border-radius: 8px; margin-bottom: 8px; font-size: 13px; }}
  .exec-summary .insight strong {{ color: #302b63; }}
  .exec-summary .action-item {{ background: #fff8e1; padding: 10px 14px; border-radius: 6px; margin-top: 8px; font-size: 13px; border-left: 3px solid #f4a261; }}
  
  /* KPI Cards */
  .kpi-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 16px; }}
  .kpi {{ background: white; border-radius: 10px; padding: 16px 12px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.08); position: relative; }}
  .kpi .value {{ font-size: 28px; font-weight: 700; color: #1a1a2e; line-height: 1.2; }}
  .kpi .label {{ color: #888; font-size: 11px; margin-top: 4px; letter-spacing: 0.5px; }}
  .kpi.highlight {{ background: linear-gradient(135deg, #0f0c29, #302b63); }}
  .kpi.highlight .value {{ color: #d4af37; }}
  .kpi.highlight .label {{ color: #b8b8d1; }}
  
  /* Sections */
  .section {{ background: white; border-radius: 10px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .section h2 {{ font-size: 15px; color: #1a1a2e; padding-bottom: 10px; margin-bottom: 14px; border-bottom: 2px solid #f0f0f0; display: flex; align-items: center; gap: 8px; }}
  .section h2 .icon {{ font-size: 18px; }}
  
  /* Tables */
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th {{ background: #f8f9fb; color: #555; padding: 8px 10px; text-align: left; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #e8e8e8; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
  tr:hover {{ background: #fafbff; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  
  /* Badges */
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; letter-spacing: 0.3px; }}
  .badge-high {{ background: #ffeaea; color: #d63031; }}
  .badge-medium {{ background: #fff4e5; color: #e17055; }}
  .badge-low {{ background: #eafaf1; color: #00b894; }}
  .badge-info {{ background: #eef2ff; color: #6c5ce7; }}
  
  /* Tags */
  .tag {{ display: inline-block; background: #f0f4ff; color: #4a6cf7; padding: 2px 7px; border-radius: 4px; font-size: 10px; margin: 1px; font-weight: 500; }}
  .tag-hot {{ background: #fff0f0; color: #e74c3c; }}
  
  /* Profit colors */
  .profit-fire {{ color: #e74c3c; font-weight: 700; }}
  .profit-high {{ color: #00b894; font-weight: 700; }}
  .profit-medium {{ color: #e17055; font-weight: 600; }}
  .profit-low {{ color: #b2bec3; }}
  
  /* Status */
  .status {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600; }}
  .status-published {{ background: #eafaf1; color: #00b894; }}
  .status-ready {{ background: #eef2ff; color: #6c5ce7; }}
  .status-pending {{ background: #fff4e5; color: #e17055; }}
  
  /* Rank */
  .rank {{ display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 50%; font-size: 11px; font-weight: 700; }}
  .rank-1 {{ background: #fff3cd; color: #d4a017; }}
  .rank-2 {{ background: #e8e8e8; color: #666; }}
  .rank-3 {{ background: #fde8d0; color: #b87333; }}
  .rank-n {{ background: #f0f0f0; color: #999; }}
  
  .divider {{ height: 1px; background: linear-gradient(to right, transparent, #d4af37, transparent); margin: 20px 0; }}
  
  .footer {{ text-align: center; color: #aaa; font-size: 11px; padding: 16px; }}
  .footer a {{ color: #6c5ce7; text-decoration: none; }}
  
  /* Responsive */
  @media (max-width: 700px) {{
    .kpi-grid {{ grid-template-columns: repeat(3, 1fr); }}
    table {{ font-size: 11px; }}
    th, td {{ padding: 6px 8px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>AQUAVERVE MARKET INTELLIGENCE</h1>
  <div class="subtitle">Terapeak 选品调研 · eBay Browse API 数据驱动</div>
  <div class="date-badge">📅 {date_str}</div>
</div>

<!-- Executive Summary (AI 智能摘要) -->
<div class="exec-summary">
  <h2>📌 今日核心发现</h2>
  <div class="insight">
    今日扫描 <strong>{summary.get('total_categories_researched', 0)}</strong> 个品类，
    其中 <strong>{summary.get('high_demand_categories', 0)}</strong> 个为高需求品类。
    库存 <strong>{inventory_total}</strong> 个产品中，
    <strong>{summary.get('inventory_products_with_opportunity', 0)}</strong> 个与热门市场匹配，
    其中 <strong>{len(high_margin_matches)}</strong> 个利润率超过 25%。"""
        
        if unpublished_opportunities:
            html += f"""
  </div>
  <div class="action-item">
    ⚡ <strong>行动建议：</strong>有 <strong>{len(unpublished_opportunities)}</strong> 个高利润产品尚未发布到 eBay，建议优先处理。"""
            gaps_count = len(data.get('category_gaps', []))
            if gaps_count > 0:
                html += f" 另有 <strong>{gaps_count}</strong> 个品类缺口可开发（库存中无匹配产品）。"
            html += """
  </div>"""
        else:
            html += "\n  </div>"
        
        html += """
</div>

<!-- KPI Cards -->
"""
        html += f"""<div class="kpi-grid">
  <div class="kpi highlight">
    <div class="value">{summary.get('categories_with_data', 0)}</div>
    <div class="label">有效品类</div>
  </div>
  <div class="kpi">
    <div class="value">{summary.get('high_demand_categories', 0)}</div>
    <div class="label">高需求品类</div>
  </div>
  <div class="kpi">
    <div class="value">{summary.get('inventory_products_with_opportunity', 0)}</div>
    <div class="label">匹配机会</div>
  </div>
  <div class="kpi">
    <div class="value" style="color:#00b894">{len(high_margin_matches)}</div>
    <div class="label">高利润 (>25%)</div>
  </div>
  <div class="kpi">
    <div class="value">${avg_market_price:.0f}</div>
    <div class="label">市场均价</div>
  </div>
</div>
"""
        
        # ========== Section 1: Top 品类（按 sell-through 排序）==========
        html += """
<div class="section">
  <h2><span class="icon">🏆</span> 高潜力品类排名</h2>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>品类</th>
        <th>Listings</th>
        <th>市场均价</th>
        <th>Top10 均价</th>
        <th>价差</th>
        <th>竞争</th>
        <th>需求</th>
        <th>价格波动</th>
      </tr>
    </thead>
    <tbody>
"""
        for idx, cat in enumerate(data.get('top_opportunities', []), 1):
            comp_class = 'badge-high' if cat.get('competition_level') == 'High' else ('badge-medium' if cat.get('competition_level') == 'Medium' else 'badge-low')
            demand = cat.get('sell_through_indicators', {}).get('estimated_demand', 'N/A')
            demand_class = 'badge-low' if demand == 'High' else ('badge-medium' if demand == 'Medium' else 'badge-info')
            rank_class = f'rank-{idx}' if idx <= 3 else 'rank-n'
            premium = cat.get('sell_through_indicators', {}).get('price_premium_vs_avg', 0)
            
            html += f"""      <tr>
        <td><span class="rank {rank_class}">{idx}</span></td>
        <td><strong>{cat.get('keyword_cn', '')}</strong><br><span style="color:#999;font-size:11px">{cat['keyword']}</span></td>
        <td>{cat.get('total_listings', 0)}</td>
        <td><strong>${cat.get('avg_price', 0):.0f}</strong></td>
        <td>${cat.get('top_10_avg_price', 0):.0f}</td>
        <td style="color:{'#00b894' if premium >= 0 else '#e74c3c'}">{'+' if premium >= 0 else ''}${premium:.0f}</td>
        <td><span class="badge {comp_class}">{cat.get('competition_level', '')}</span></td>
        <td><span class="badge {demand_class}">{demand}</span></td>
        <td>{cat.get('price_spread', 0):.0%}</td>
      </tr>
"""
        html += "    </tbody>\n  </table>\n</div>\n"
        
        # ========== Section 2: 热门关键词 ==========
        html += """
<div class="section">
  <h2><span class="icon">🔑</span> 各品类 Top 关键词（用于标题优化）</h2>
  <table>
    <thead>
      <tr><th>品类</th><th>高频关键词（出现次数）</th></tr>
    </thead>
    <tbody>
"""
        for cat in data.get('top_opportunities', [])[:12]:
            tags = ""
            for word, count in cat.get('top_keywords', [])[:10]:
                tag_cls = 'tag-hot' if count >= 10 else 'tag'
                tags += f'<span class="{tag_cls}">{word} ({count})</span> '
            if tags:
                html += f"      <tr><td><strong>{cat.get('keyword_cn', '')}</strong></td><td>{tags}</td></tr>\n"
        
        html += "    </tbody>\n  </table>\n</div>\n"
        
        # ========== Section 3: 畅销标题 ==========
        html += """
<div class="section">
  <h2><span class="icon">🔥</span> 各品类 Best Match 畅销标题</h2>
  <table>
    <thead>
      <tr><th style="width:100px">品类</th><th style="width:30px">#</th><th>畅销标题（学习关键词模式）</th></tr>
    </thead>
    <tbody>
"""
        for cat in data.get('top_opportunities', [])[:8]:
            titles = cat.get('top_titles', [])
            for i, title in enumerate(titles[:3], 1):
                rank_cls = f'rank-{i}' if i <= 3 else 'rank-n'
                html += f'      <tr><td>{cat.get("keyword_cn", "")}</td><td><span class="rank {rank_cls}">{i}</span></td><td style="font-size:12px">{title}</td></tr>\n'
        
        html += "    </tbody>\n  </table>\n</div>\n"
        
        # ========== Section 4: 库存匹配（最重要的 actionable 部分）==========
        inventory_matches = data.get('inventory_matches', [])
        if inventory_matches:
            html += f"""
<div class="section">
  <h2><span class="icon">💰</span> 库存产品 × 市场机会匹配 <span style="font-size:12px;color:#999;font-weight:400">（{len(inventory_matches)} 个匹配）</span></h2>
  
  <div style="display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap">"""
            for label, count in margin_groups.items():
                html += f'    <div style="background:#f8f9fb;padding:6px 14px;border-radius:6px;font-size:12px">{label}: <strong>{count}</strong></div>\n'
            html += "  </div>\n"
            
            html += """  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>SKU</th>
        <th>产品名称</th>
        <th>状态</th>
        <th>匹配品类</th>
        <th>成本</th>
        <th>市场均价</th>
        <th>eBay费用</th>
        <th>净利润</th>
        <th>净利润率</th>
      </tr>
    </thead>
    <tbody>
"""
            for idx, match in enumerate(inventory_matches[:40], 1):
                margin = match.get('potential_margin', 0)
                if margin >= 50:
                    profit_cls = 'profit-fire'
                elif margin >= 25:
                    profit_cls = 'profit-high'
                elif margin >= 15:
                    profit_cls = 'profit-medium'
                else:
                    profit_cls = 'profit-low'
                
                status = match.get('status', '')
                status_cls = 'status-published' if status == 'PUBLISHED' else ('status-ready' if status == 'READY' else 'status-pending')
                
                title_display = match['title']
                if len(title_display) > 50:
                    title_display = title_display[:50] + '…'
                
                net_profit = match.get('net_profit', 0)
                profit_color = '#00b894' if net_profit > 0 else '#e74c3c'
                
                html += f"""      <tr>
        <td>{idx}</td>
        <td style="font-family:monospace;font-size:11px">{match['sku']}</td>
        <td>{title_display}</td>
        <td><span class="status {status_cls}">{status}</span></td>
        <td><span class="tag">{match.get('market_keyword', '')}</span></td>
        <td>${match.get('cost', 0):.0f}</td>
        <td>${match.get('market_avg', 0):.0f}</td>
        <td style="color:#888;font-size:11px">-${match.get('ebay_fees', 0):.0f}</td>
        <td style="color:{profit_color};font-weight:600">${net_profit:.0f}</td>
        <td class="{profit_cls}">{margin:.0f}%</td>
      </tr>
"""
            
            html += "    </tbody>\n  </table>\n</div>\n"
        
        # ========== Section 4c: 市场智能机会快照（来自 Streamlit MI 页面）==========
        mi = data.get("mi_opportunities") or {}
        if mi.get("available") and mi.get("opportunities"):
            opps = mi["opportunities"]
            snap_meta = []
            if mi.get("generated_at"):
                snap_meta.append(f"生成: {mi['generated_at'][:19].replace('T',' ')}")
            if mi.get("age_hours") is not None:
                snap_meta.append(f"{mi['age_hours']}h 前")
            if mi.get("min_margin") is not None:
                try:
                    snap_meta.append(f"最低利润率 {float(mi['min_margin'])*100:.0f}%")
                except Exception:
                    pass
            meta_str = " · ".join(snap_meta) if snap_meta else ""

            html += f"""
<div class="section" style="border-left:4px solid #6c5ce7">
  <h2><span class="icon">🧠</span> 市场智能机会快照 <span style="font-size:12px;color:#999;font-weight:400">（{len(opps)}/{mi.get('total_in_snapshot', len(opps))} 条 · {meta_str}）</span></h2>
    <p style="color:#666;font-size:13px;margin-bottom:12px">来自 Streamlit Market Intelligence 页面的 <code>auto_discover_opportunities</code> 输出，只包含尚未刊登的本地候选，按机会分数排序。</p>
  <p style="color:#b58900;font-size:12px;margin:0 0 12px 0;background:#fff8e1;padding:6px 10px;border-radius:4px;border-left:3px solid #f0c419">⚠️ Marketplace Insights API 已被 eBay 拒绝；STR / 需求分均为 Browse 估算，非真实成交率。</p>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>SKU</th>
        <th>状态</th>
        <th>标题</th>
        <th>机会分</th>
        <th>成本$</th>
        <th>建议价$</th>
        <th>利润$</th>
        <th>利润率</th>
        <th>竞争</th>
      </tr>
    </thead>
    <tbody>
"""
            for idx, o in enumerate(opps, 1):
                title_disp = (o.get("title", "") or "")
                if len(title_disp) > 50:
                    title_disp = title_disp[:50] + '…'
                score = o.get("opportunity_score", 0)
                if score >= 80: badge = "🔥"
                elif score >= 65: badge = "✅"
                else: badge = "⚠️"
                status = o.get("status", "")
                status_cls = 'status-published' if status == 'PUBLISHED' else ('status-ready' if status == 'READY' else 'status-pending')
                html += f"""      <tr>
        <td>{idx}</td>
        <td style="font-family:monospace;font-size:11px">{o.get('sku', '')}</td>
        <td><span class="status {status_cls}">{status}</span></td>
        <td>{title_disp}</td>
        <td>{badge} <strong>{score}</strong></td>
        <td>${o.get('dajian_cost', 0):.0f}</td>
        <td>${o.get('suggested_price', 0):.0f}</td>
        <td style="color:#00b894;font-weight:600">${o.get('potential_profit', 0):.0f}</td>
        <td>{o.get('margin_rate', 0):.1f}%</td>
        <td>{(o.get('competition', '') or '').upper()}</td>
      </tr>
"""
            html += "    </tbody>\n  </table>\n</div>\n"
        elif mi.get("reason"):
            html += f'<div class="section"><p style="color:#999;font-size:12px">📊 市场智能快照: {mi["reason"]}</p></div>\n'

        # ========== Section 4b: 未刊登高潜力产品（需要行动）==========
        unpublished = data.get('unpublished_priority', [])
        if unpublished:
            html += f"""
<div class="section" style="border-left:4px solid #e74c3c">
  <h2><span class="icon">🚨</span> 未刊登高潜力产品 <span style="font-size:12px;color:#e74c3c;font-weight:400">（{len(unpublished)} 个待刊登）</span></h2>
  <p style="font-size:12px;color:#666;margin-bottom:12px">以下产品有市场需求且利润率 ≥15%，但尚未发布到 eBay，建议优先刊登。</p>
  <table>
    <thead>
      <tr><th>#</th><th>SKU</th><th>产品名称</th><th>状态</th><th>匹配品类</th><th>成本</th><th>市场均价</th><th>净利润</th><th>净利润率</th></tr>
    </thead>
    <tbody>
"""
            for idx, m in enumerate(unpublished[:15], 1):
                margin = m.get('potential_margin', 0)
                profit_cls = 'profit-fire' if margin >= 50 else ('profit-high' if margin >= 25 else 'profit-medium')
                status_cls = 'status-ready' if m.get('status') == 'READY' else 'status-pending'
                title_display = m['title'][:50] + '…' if len(m['title']) > 50 else m['title']
                net_profit = m.get('net_profit', 0)
                html += f"""      <tr>
        <td>{idx}</td>
        <td style="font-family:monospace;font-size:11px">{m['sku']}</td>
        <td>{title_display}</td>
        <td><span class="status {status_cls}">{m.get('status','')}</span></td>
        <td><span class="tag">{m.get('market_keyword','')}</span></td>
        <td>${m.get('cost',0):.0f}</td>
        <td>${m.get('market_avg',0):.0f}</td>
        <td style="color:{'#00b894' if net_profit > 0 else '#e74c3c'};font-weight:600">${net_profit:.0f}</td>
        <td class="{profit_cls}">{margin:.0f}%</td>
      </tr>
"""
            html += "    </tbody>\n  </table>\n</div>\n"
        
        # ========== Section 4c: 品类缺口分析（选品机会）==========
        category_gaps = data.get('category_gaps', [])
        if category_gaps:
            html += f"""
<div class="section" style="border-left:4px solid #6c5ce7">
  <h2><span class="icon">🔎</span> 品类缺口 · 选品机会 <span style="font-size:12px;color:#6c5ce7;font-weight:400">（{len(category_gaps)} 个待开发品类）</span></h2>
  <p style="font-size:12px;color:#666;margin-bottom:12px">以下品类在 eBay 上有市场需求，但您的大建库存中没有或很少有匹配产品。建议在大建平台上搜索并采集相关产品。</p>
  <table>
    <thead>
      <tr><th>品类</th><th>市场均价</th><th>Top10 均价</th><th>竞争</th><th>Listings</th><th>缺口原因</th></tr>
    </thead>
    <tbody>
"""
            for gap in category_gaps:
                comp_class = 'badge-high' if gap.get('competition_level') == 'High' else ('badge-medium' if gap.get('competition_level') == 'Medium' else 'badge-low')
                html += f"""      <tr>
        <td><strong>{gap.get('keyword_cn','')}</strong><br><span style="color:#999;font-size:11px">{gap['keyword']}</span></td>
        <td>${gap.get('avg_price',0):.0f}</td>
        <td>${gap.get('top_10_avg_price',0):.0f}</td>
        <td><span class="badge {comp_class}">{gap.get('competition_level','')}</span></td>
        <td>{gap.get('total_listings',0)}</td>
        <td style="font-size:11px;color:#6c5ce7">{gap.get('reason','')}</td>
      </tr>
"""
            # 添加畅销标题参考
            html += "    </tbody>\n  </table>\n"
            html += '  <div style="margin-top:12px;padding:12px;background:#f8f7ff;border-radius:6px">\n'
            html += '    <div style="font-size:12px;font-weight:600;color:#302b63;margin-bottom:8px">📝 缺口品类畅销标题参考（供选品时参考）</div>\n'
            for gap in category_gaps[:5]:
                titles = gap.get('top_titles', [])
                if titles:
                    html += f'    <div style="font-size:11px;margin-bottom:4px"><strong>{gap.get("keyword_cn","")}：</strong></div>\n'
                    for t in titles[:2]:
                        html += f'    <div style="font-size:11px;color:#666;padding-left:12px;margin-bottom:2px">• {t}</div>\n'
            html += "  </div>\n</div>\n"
        
        # ========== Section 5: 全品类一览 ==========
        html += """
<div class="section">
  <h2><span class="icon">📋</span> 全品类市场数据</h2>
  <table>
    <thead>
      <tr>
        <th>品类</th>
        <th>关键词</th>
        <th>Listings</th>
        <th>均价</th>
        <th>中位价</th>
        <th>价格波动</th>
        <th>竞争</th>
      </tr>
    </thead>
    <tbody>
"""
        for cat in sorted(data.get('categories', []), key=lambda c: c.get('avg_price', 0), reverse=True):
            if cat.get('total_listings', 0) == 0:
                continue
            comp_class = 'badge-high' if cat.get('competition_level') == 'High' else ('badge-medium' if cat.get('competition_level') == 'Medium' else 'badge-low')
            
            html += f"""      <tr>
        <td>{cat.get('keyword_cn', '')}</td>
        <td>{cat['keyword']}</td>
        <td>{cat.get('total_listings', 0)}</td>
        <td>${cat.get('avg_price', 0):.0f}</td>
        <td>${cat.get('median_price', 0):.0f}</td>
        <td>{cat.get('price_spread', 0):.0%}</td>
        <td><span class="badge {comp_class}">{cat.get('competition_level', '')}</span></td>
      </tr>
"""
        html += "    </tbody>\n  </table>\n</div>\n"
        
        # Footer
        html += f"""
<div class="footer">
  AquaVerve Market Intelligence · {datetime.now().strftime('%Y-%m-%d %H:%M')} · eBay Browse API<br>
  <span style="font-size:10px">排序依据: Best Match（eBay 内部算法综合销量、转化率、相关性等因素）· Top10均价反映畅销价位</span>
</div>

</body>
</html>"""
        
        return html
    
    def save_report(self) -> Path:
        """保存报告到文件"""
        REPORT_DIR.mkdir(exist_ok=True)
        
        date_str = datetime.now().strftime("%Y%m%d")
        
        # 保存 JSON
        json_path = REPORT_DIR / f"terapeak_report_{date_str}.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(self.report_data, f, ensure_ascii=False, indent=2)
        
        # 保存 HTML
        html_path = REPORT_DIR / f"terapeak_report_{date_str}.html"
        html_content = self.generate_html_report()
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        print(f"\n📁 报告已保存:")
        print(f"   JSON: {json_path}")
        print(f"   HTML: {html_path}")
        
        return html_path
    
    def send_email(self, html_path: Path = None):
        """发送邮件报告（使用统一邮件模块）"""
        from src.utils.email_sender import send_email
        
        config = EMAIL_CONFIG
        if not config['sender_email'] or not config['sender_password'] or not config['recipient_email']:
            print("⚠️ 邮件配置不完整，跳过发送。请在 .env 中设置:")
            print("   NOTIFICATION_EMAIL, NOTIFICATION_EMAIL_PASSWORD")
            return False
        
        html_content = self.generate_html_report()
        summary = self.report_data.get('summary', {})
        high_margin_count = len([m for m in self.report_data.get('inventory_matches', []) if m.get('potential_margin', 0) >= 25])
        
        subject = f"📊 AquaVerve 市场情报 - {self.report_data.get('date', '')} | {high_margin_count} 高利润产品 | {summary.get('categories_with_data', 0)} 品类"
        
        return send_email(
            subject=subject,
            html_body=html_content,
            to_email=config['recipient_email']
        )


def main():
    import argparse
    parser = argparse.ArgumentParser(description="每日 Terapeak 市场调研报告")
    parser.add_argument('--email', action='store_true', help='发送邮件报告')
    parser.add_argument('--categories', type=int, default=None, help='限制研究品类数量（测试用）')
    args = parser.parse_args()
    
    # 如果限制了品类数量
    global RESEARCH_CATEGORIES
    if args.categories:
        RESEARCH_CATEGORIES = RESEARCH_CATEGORIES[:args.categories]
    
    reporter = DailyTerapeakReport()
    
    # 生成报告
    report_data = reporter.generate_report()
    
    # 保存报告
    html_path = reporter.save_report()
    
    # 发送邮件
    if args.email:
        reporter.send_email(html_path)
    
    print(f"\n✅ 报告生成完成!")
    print(f"   用浏览器打开 HTML 报告查看详情: {html_path}")


if __name__ == "__main__":
    main()
