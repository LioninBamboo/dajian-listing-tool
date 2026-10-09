"""
测试自动爆品发掘功能
"""
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

from src.plugins.terapeak_research.intelligence_service import IntelligenceService

def test_auto_discover():
    print("=" * 70)
    print("🔥 自动爆品发掘测试")
    print("=" * 70)
    
    intel = IntelligenceService()
    
    print("\n📊 正在分析库存产品与市场数据...\n")
    
    opportunities = intel.auto_discover_opportunities(
        min_margin=0.15,  # 15% 最低利润率
        max_results=10
    )
    
    if not opportunities:
        print("❌ 未发现满足条件的高潜力产品")
        print("可能原因: 库存产品较少或成本较高")
        return
    
    print(f"✅ 发现 {len(opportunities)} 个高潜力产品!\n")
    print("-" * 70)
    
    for i, opp in enumerate(opportunities, 1):
        score = opp['opportunity_score']
        score_emoji = "🔥" if score >= 80 else "✅" if score >= 65 else "⚠️"
        
        print(f"\n{score_emoji} #{i} SKU: {opp['sku']}")
        print(f"   标题: {opp['title'][:50]}...")
        print(f"   搜索关键词: {opp['search_keywords']}")
        print(f"   大建成本: ${opp['dajian_cost']:.2f}")
        print(f"   市场均价: ${opp['market_avg_price']:.2f}")
        print(f"   建议售价: ${opp['suggested_price']:.2f}")
        print(f"   潜在利润: ${opp['potential_profit']:.2f} ({opp['margin_rate']:.1f}%)")
        print(f"   竞争程度: {opp['competition']}")
        print(f"   机会分数: {score}/100")
        print(f"   推荐: {opp['recommendation']}")
    
    print("\n" + "=" * 70)
    
    # 统计
    high_score = [o for o in opportunities if o['opportunity_score'] >= 70]
    avg_margin = sum(o['margin_rate'] for o in opportunities) / len(opportunities)
    total_profit = sum(o['potential_profit'] for o in opportunities)
    
    print(f"\n📈 汇总统计:")
    print(f"   强烈推荐产品: {len(high_score)} 个")
    print(f"   平均利润率: {avg_margin:.1f}%")
    print(f"   潜在总利润: ${total_profit:,.2f}")

if __name__ == "__main__":
    test_auto_discover()
