"""
测试 Terapeak API 调用是否正常工作
"""
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

from src.plugins.terapeak_research.research_client import TerapeakClient
from src.plugins.terapeak_research.intelligence_service import IntelligenceService

def test_api():
    print("=" * 60)
    print("Terapeak/eBay Browse API 测试 (修复后)")
    print("=" * 60)
    
    client = TerapeakClient()
    
    # 测试 1: 搜索 sectional sofa (使用正确的过滤)
    print("\n📍 测试关键词: 'sectional sofa' (Buy It Now, $100+)")
    results = client.search_products(
        'sectional sofa', 
        limit=20,
        sort="bestMatch",
        min_price=100,
        buy_it_now_only=True
    )
    
    if not results:
        print("❌ 没有返回结果 - API 调用可能失败")
    else:
        print(f"✅ 返回 {len(results)} 个结果\n")
        
        prices = [r['price'] for r in results if r.get('price', 0) > 0]
        
        if prices:
            print("💰 价格统计:")
            print(f"   最低价: ${min(prices):,.2f}")
            print(f"   最高价: ${max(prices):,.2f}")
            print(f"   平均价: ${sum(prices)/len(prices):,.2f}")
            print(f"   中位价: ${sorted(prices)[len(prices)//2]:,.2f}")
        
        print("\n📦 前5个结果:")
        for i, item in enumerate(results[:5], 1):
            price = item.get('price', 0)
            title = item.get('title', 'N/A')[:55]
            print(f"   {i}. ${price:,.2f} - {title}...")
    
    # 测试 2: 使用 IntelligenceService 的 analyze_market
    print("\n" + "=" * 60)
    print("📊 测试 IntelligenceService.analyze_market()")
    print("=" * 60)
    
    intel = IntelligenceService()
    
    for kw in ['sectional sofa', 'gaming chair', 'dining table']:
        print(f"\n🔍 关键词: '{kw}'")
        market = intel.analyze_market(kw)
        print(f"   平均价: ${market.avg_price:,.2f}")
        print(f"   中位价: ${market.median_price:,.2f}")
        print(f"   价格区间: ${market.min_price:,.2f} - ${market.max_price:,.2f}")
        print(f"   竞争强度: {market.competition_level}")
    
    print("\n" + "=" * 60)

if __name__ == "__main__":
    test_api()
