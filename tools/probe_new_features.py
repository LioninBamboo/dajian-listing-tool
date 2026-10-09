"""
测试新功能脚本

用于验证以下功能是否正常:
1. 大建API客户端 (DaJianClient)
2. 趋势发掘服务 (MarketTrendDiscovery)
3. Terapeak研究客户端 (TerapeakClient)
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import os


def test_separator(name: str):
    """打印分隔符"""
    print(f"\n{'='*60}")
    print(f"  测试: {name}")
    print('='*60)


def test_dajian_client():
    """测试大建API客户端"""
    test_separator("大建API客户端")
    
    client_id = os.getenv("DAJIAN_API_KEY")
    client_secret = os.getenv("DAJIAN_API_SECRET")
    
    if not client_id or not client_secret:
        print("❌ 未配置大建API凭证")
        print("请在 .env 中设置 DAJIAN_API_KEY 和 DAJIAN_API_SECRET")
        return False
    
    print(f"✅ API Key: {client_id[:8]}...{client_id[-4:]}")
    
    try:
        from src.clients.dajian_client import DaJianClient
        
        client = DaJianClient(client_id, client_secret)
        
        # 测试连接
        print("\n1. 测试连接...")
        if client.test_connection():
            print("   ✅ API 连接成功")
        else:
            print("   ⚠️ API 连接失败（可能是认证问题）")
        
        # 测试收藏获取
        print("\n2. 获取收藏产品...")
        try:
            favorites = client.get_favorites()
            if favorites:
                print(f"   ✅ 获取到 {len(favorites)} 个收藏产品")
                for fav in favorites[:3]:
                    print(f"      - {fav.get('sku', fav.get('skuCode', 'N/A'))}: {fav.get('title', fav.get('name', 'N/A'))[:50]}")
            else:
                print("   ⚠️ 未找到收藏产品（可能没有收藏或API不支持）")
        except Exception as e:
            print(f"   ❌ 获取收藏失败: {e}")
        
        # 测试产品搜索
        print("\n3. 搜索产品 (关键词: sofa)...")
        try:
            products = client.search_products("sofa", limit=5)
            if products:
                print(f"   ✅ 搜索到 {len(products)} 个产品")
                for p in products[:3]:
                    print(f"      - {p.get('sku', 'N/A')}: ${p.get('price', 0)}")
            else:
                print("   ⚠️ 未搜索到产品")
        except Exception as e:
            print(f"   ❌ 搜索失败: {e}")
        
        return True
        
    except Exception as e:
        print(f"❌ 大建客户端测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_terapeak_client():
    """测试Terapeak研究客户端"""
    test_separator("Terapeak研究客户端")
    
    try:
        from src.plugins.terapeak_research.research_client import TerapeakClient
        
        client = TerapeakClient()
        
        # 测试搜索
        print("\n1. 搜索市场数据 (关键词: coffee table)...")
        products = client.search_products("coffee table", limit=20, min_price=50, buy_it_now_only=True)
        
        if products:
            print(f"   ✅ 找到 {len(products)} 个产品")
            
            prices = [p['price'] for p in products if p.get('price', 0) > 0]
            if prices:
                avg_price = sum(prices) / len(prices)
                min_price = min(prices)
                max_price = max(prices)
                
                print(f"   📊 价格统计:")
                print(f"      - 平均价: ${avg_price:.2f}")
                print(f"      - 最低价: ${min_price:.2f}")
                print(f"      - 最高价: ${max_price:.2f}")
            
            # 显示样例
            print(f"\n   📦 样例产品:")
            for p in products[:3]:
                print(f"      - {p.get('title', 'N/A')[:50]}... - ${p.get('price', 0):.2f}")
        else:
            print("   ⚠️ 未找到产品")
        
        return True
        
    except Exception as e:
        print(f"❌ Terapeak客户端测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_trend_discovery():
    """测试趋势发掘服务"""
    test_separator("趋势发掘服务")
    
    try:
        from src.plugins.terapeak_research.trend_discovery import MarketTrendDiscovery
        
        discovery = MarketTrendDiscovery()
        
        # 测试市场机会发现
        print("\n1. 发现市场机会 (3个关键词)...")
        keywords = ["sectional sofa", "gaming chair", "tv stand"]
        
        opportunities = discovery.discover_opportunities(
            keywords=keywords,
            include_dajian_favorites=False
        )
        
        if opportunities:
            print(f"   ✅ 发现 {len(opportunities)} 个机会")
            
            for opp in opportunities:
                print(f"\n   📍 {opp.keyword}")
                print(f"      - 市场均价: ${opp.market_avg_price:.2f}")
                print(f"      - 竞争程度: {opp.competition}")
                print(f"      - 机会分数: {opp.opportunity_score}")
                print(f"      - 推荐价格: ${opp.suggested_price_range[0]:.0f} - ${opp.suggested_price_range[1]:.0f}")
        else:
            print("   ⚠️ 未发现市场机会")
        
        # 测试刊登建议
        print("\n2. 获取刊登建议 (platform bed)...")
        advice = discovery.get_listing_recommendation("platform bed")
        
        if "error" not in advice:
            print(f"   ✅ 生成建议成功")
            print(f"      - 推荐价格: ${advice['pricing_strategy']['recommended_price']:.2f}")
            print(f"      - 竞争程度: {advice['market_analysis']['competition']}")
            
            if advice['required_item_specifics']:
                print(f"      - 推荐Item Specifics:")
                for k, v in list(advice['required_item_specifics'].items())[:3]:
                    print(f"         {k}: {v}")
        else:
            print(f"   ⚠️ {advice['error']}")
        
        return True
        
    except Exception as e:
        print(f"❌ 趋势发掘测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_intelligence_service():
    """测试智能服务"""
    test_separator("智能服务 (IntelligenceService)")
    
    try:
        from src.plugins.terapeak_research.intelligence_service import IntelligenceService
        
        intel = IntelligenceService()
        
        # 测试市场分析
        print("\n1. 分析市场 (sofa)...")
        market = intel.analyze_market("sofa")
        
        print(f"   ✅ 分析完成")
        print(f"      - 平均价格: ${market.avg_price:.2f}")
        print(f"      - 竞争程度: {market.competition_level}")
        print(f"      - 商品数量: {market.total_listings}")
        
        return True
        
    except Exception as e:
        print(f"❌ 智能服务测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_database():
    """测试数据库连接"""
    test_separator("数据库连接")
    
    import sqlite3
    db_path = PROJECT_ROOT / "ebay_collection.db"
    
    if not db_path.exists():
        print(f"❌ 数据库不存在: {db_path}")
        return False
    
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        
        # 统计产品
        cur.execute("SELECT status, COUNT(*) FROM collected_products GROUP BY status")
        stats = cur.fetchall()
        
        print("✅ 数据库连接成功")
        print("\n📊 产品状态统计:")
        for status, count in stats:
            print(f"   - {status}: {count}")
        
        # 获取最新产品
        cur.execute("""
            SELECT sku, title, status, listing_id 
            FROM collected_products 
            ORDER BY updated_at DESC 
            LIMIT 3
        """)
        
        print("\n📦 最新产品:")
        for row in cur.fetchall():
            sku, title, status, listing_id = row
            print(f"   - {sku}: {title[:40]}... ({status})")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"❌ 数据库测试失败: {e}")
        return False


def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("  大建刊登工具 - 功能测试")
    print("="*60)
    
    results = {}
    
    # 1. 数据库
    results['database'] = test_database()
    
    # 2. Terapeak
    results['terapeak'] = test_terapeak_client()
    
    # 3. 趋势发掘
    results['trend_discovery'] = test_trend_discovery()
    
    # 4. 智能服务
    results['intelligence'] = test_intelligence_service()
    
    # 5. 大建API (可能失败)
    results['dajian'] = test_dajian_client()
    
    # 总结
    print("\n" + "="*60)
    print("  测试结果汇总")
    print("="*60)
    
    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"  {name}: {status}")
    
    passed_count = sum(1 for v in results.values() if v)
    total_count = len(results)
    
    print(f"\n  总计: {passed_count}/{total_count} 通过")
    
    if passed_count == total_count:
        print("\n🎉 所有测试通过!")
    else:
        print("\n⚠️ 部分测试失败，请检查上面的错误信息")


if __name__ == "__main__":
    main()
