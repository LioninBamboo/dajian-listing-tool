"""
简单测试脚本 - 不调用大建API
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

print("="*50)
print("  简单功能测试")
print("="*50)

# 1. 数据库测试
print("\n1. 数据库测试...")
import sqlite3
try:
    conn = sqlite3.connect(str(PROJECT_ROOT / 'ebay_collection.db'))
    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) FROM collected_products GROUP BY status")
    for row in cur.fetchall():
        print(f"   {row[0]}: {row[1]}")
    conn.close()
    print("   ✅ 数据库 OK")
except Exception as e:
    print(f"   ❌ 失败: {e}")

# 2. Terapeak 测试
print("\n2. Terapeak API 测试...")
try:
    from src.plugins.terapeak_research.research_client import TerapeakClient
    client = TerapeakClient()
    products = client.search_products("coffee table", limit=10, min_price=50, buy_it_now_only=True)
    if products:
        prices = [p['price'] for p in products if p.get('price')]
        avg = sum(prices) / len(prices) if prices else 0
        print(f"   找到 {len(products)} 个产品, 均价 ${avg:.2f}")
        print("   ✅ Terapeak OK")
    else:
        print("   ⚠️ 无结果")
except Exception as e:
    print(f"   ❌ 失败: {e}")

# 3. 趋势发掘测试
print("\n3. 趋势发掘测试...")
try:
    from src.plugins.terapeak_research.trend_discovery import MarketTrendDiscovery
    discovery = MarketTrendDiscovery()
    opps = discovery.discover_opportunities(['gaming chair'], include_dajian_favorites=False)
    if opps:
        opp = opps[0]
        print(f"   {opp.keyword}: 机会分数={opp.opportunity_score}, 均价=${opp.market_avg_price:.2f}")
        print("   ✅ 趋势发掘 OK")
    else:
        print("   ⚠️ 无结果")
except Exception as e:
    print(f"   ❌ 失败: {e}")

# 4. Item Specifics 建议测试
print("\n4. Item Specifics 建议测试...")
try:
    from src.plugins.terapeak_research.trend_discovery import MarketTrendDiscovery
    discovery = MarketTrendDiscovery()
    advice = discovery.get_listing_recommendation("sectional sofa")
    if "error" not in advice:
        print(f"   推荐价格: ${advice['pricing_strategy']['recommended_price']:.2f}")
        print(f"   竞争程度: {advice['market_analysis']['competition']}")
        if advice['required_item_specifics']:
            print(f"   Item Specifics: {advice['required_item_specifics']}")
        print("   ✅ Item Specifics 建议 OK")
    else:
        print(f"   ⚠️ {advice['error']}")
except Exception as e:
    print(f"   ❌ 失败: {e}")

print("\n" + "="*50)
print("  测试完成")
print("="*50)
