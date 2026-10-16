"""
产品调研命令行工具

用法:
    python research_cli.py keyword "sofa" "dining table"
    python research_cli.py category 262028
    python research_cli.py hot-products --min-price 100 --max-price 500
    python research_cli.py opportunities
"""
import sys
import os
import argparse
import json
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")


def cmd_keyword(args):
    """关键词调研"""
    from research_client import ProductResearcher
    
    researcher = ProductResearcher()
    
    for kw in args.keywords:
        print(f"\n{'='*50}")
        print(f"调研关键词: {kw}")
        print('='*50)
        
        result = researcher.research_keyword(kw)
        
        comp = result['competition']
        print(f"\n📊 竞争分析:")
        print(f"   竞争程度: {comp.get('competition_level', 'N/A')}")
        print(f"   商品数量: {comp.get('total_listings', 'N/A')}")
        print(f"   平均价格: ${comp.get('avg_price', 0):.2f}")
        print(f"   价格区间: {comp.get('price_range', 'N/A')}")
        print(f"   💡 建议: {comp.get('recommendation', 'N/A')}")
        
        pa = result['price_analysis']
        print(f"\n💰 价格统计:")
        print(f"   样本数: {pa['count']}")
        print(f"   平均: ${pa['avg']:.2f}")
        print(f"   区间: ${pa['min']:.2f} - ${pa['max']:.2f}")
        
        if result['sample_products']:
            print(f"\n🛒 示例产品:")
            for i, p in enumerate(result['sample_products'][:3], 1):
                print(f"   {i}. ${p['price']:.2f} - {p['title'][:50]}...")


def cmd_category(args):
    """分类趋势分析"""
    from research_client import TerapeakClient
    
    client = TerapeakClient()
    
    print(f"\n分析分类: {args.category_id}")
    print('='*50)
    
    trends = client.get_category_trends(args.category_id)
    
    if 'error' in trends:
        print(f"❌ 错误: {trends['error']}")
        return
    
    print(f"📊 分类统计:")
    print(f"   总商品数: {trends.get('total_listings', 'N/A')}")
    print(f"   样本数: {trends.get('sample_size', 'N/A')}")
    print(f"   平均价格: ${trends.get('avg_price', 0):.2f}")
    print(f"   价格区间: ${trends.get('min_price', 0):.2f} - ${trends.get('max_price', 0):.2f}")
    print(f"   中位价格: ${trends.get('price_median', 0):.2f}")


def cmd_hot_products(args):
    """查找热销产品"""
    from research_client import TerapeakClient
    
    client = TerapeakClient()
    
    print(f"\n查找热销产品...")
    print(f"价格区间: ${args.min_price} - ${args.max_price}")
    print('='*50)
    
    products = client.find_hot_products(
        category_id=args.category,
        min_price=args.min_price,
        max_price=args.max_price
    )
    
    if not products:
        print("未找到符合条件的产品")
        return
    
    print(f"\n找到 {len(products)} 个产品:\n")
    
    for i, p in enumerate(products[:10], 1):
        print(f"{i}. ${p.price:.2f} - {p.title[:60]}...")
        print(f"   卖家: {p.seller_username} | 状态: {p.condition}")
        print(f"   链接: {p.item_url}")
        print()


def cmd_opportunities(args):
    """发现商机 (结合大建产品)"""
    import sqlite3
    from research_client import ProductResearcher
    
    print("\n🔍 分析大建产品市场机会...")
    print('='*50)
    
    # 从数据库获取大建产品
    db_path = PROJECT_ROOT / "ebay_collection.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    # 获取待发布或已发布的产品
    cur.execute("""
        SELECT sku, title, cost_breakdown 
        FROM collected_products 
        WHERE status IN ('PENDING', 'READY', 'PUBLISHED')
        LIMIT ?
    """, (args.limit,))
    
    products = []
    for row in cur.fetchall():
        cost = json.loads(row['cost_breakdown']) if row['cost_breakdown'] else {}
        products.append({
            'sku': row['sku'],
            'title': row['title'],
            'price': cost.get('product_price', 0)
        })
    
    conn.close()
    
    if not products:
        print("未找到产品数据")
        return
    
    print(f"分析 {len(products)} 个产品...\n")
    
    researcher = ProductResearcher()
    opportunities = researcher.find_opportunities(products[:args.limit])
    
    print(f"\n📈 机会排名:\n")
    
    for i, opp in enumerate(opportunities[:10], 1):
        print(f"{i}. {opp['recommendation']}")
        print(f"   SKU: {opp['sku']}")
        print(f"   标题: {opp['title'][:50]}...")
        print(f"   大建价: ${opp['dajian_price']:.2f} | 市场均价: ${opp['market_avg_price']:.2f}")
        print(f"   潜在利润: ${opp['potential_margin']:.2f}")
        print(f"   竞争: {opp['competition']} | 分数: {opp['opportunity_score']}")
        print()


def cmd_report(args):
    """生成调研报告"""
    from research_client import ProductResearcher
    
    print(f"\n生成调研报告...")
    
    researcher = ProductResearcher()
    report = researcher.generate_report(args.keywords)
    
    # 保存报告
    report_dir = PROJECT_ROOT / "reports"
    report_dir.mkdir(exist_ok=True)
    
    filename = f"market_research_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    report_path = report_dir / filename
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"✅ 报告已保存: {report_path}")
    print(f"\n{report}")


def main():
    parser = argparse.ArgumentParser(description='eBay 产品调研工具')
    subparsers = parser.add_subparsers(dest='command', help='可用命令')
    
    # keyword 命令
    kw_parser = subparsers.add_parser('keyword', help='关键词调研')
    kw_parser.add_argument('keywords', nargs='+', help='要调研的关键词')
    kw_parser.set_defaults(func=cmd_keyword)
    
    # category 命令
    cat_parser = subparsers.add_parser('category', help='分类趋势分析')
    cat_parser.add_argument('category_id', help='eBay 分类 ID')
    cat_parser.set_defaults(func=cmd_category)
    
    # hot-products 命令
    hot_parser = subparsers.add_parser('hot-products', help='查找热销产品')
    hot_parser.add_argument('--min-price', type=float, default=50, help='最低价格')
    hot_parser.add_argument('--max-price', type=float, default=500, help='最高价格')
    hot_parser.add_argument('--category', help='分类 ID (可选)')
    hot_parser.set_defaults(func=cmd_hot_products)
    
    # opportunities 命令
    opp_parser = subparsers.add_parser('opportunities', help='发现商机')
    opp_parser.add_argument('--limit', type=int, default=10, help='分析产品数量')
    opp_parser.set_defaults(func=cmd_opportunities)
    
    # report 命令
    rep_parser = subparsers.add_parser('report', help='生成调研报告')
    rep_parser.add_argument('keywords', nargs='+', help='要调研的关键词')
    rep_parser.set_defaults(func=cmd_report)
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    args.func(args)


if __name__ == "__main__":
    main()
