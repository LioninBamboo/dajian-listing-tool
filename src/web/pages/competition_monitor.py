"""
竞争监控 Dashboard 页面

Streamlit 页面：持续监控在售链接健康状况

主要功能:
1. 品类竞争热力图 — 按竞争强度+利润率可视化
2. 链接健康分数排行 — 底部链接早期预警 (含展示/点击/销售数据)
3. 一键下架 — 选择并下架表现差的链接 (有销售记录的自动保护)
4. 品类深入分析 — 单个品类的详细数据
5. 趋势追踪 — 加载历史报告对比
"""
import streamlit as st
import pandas as pd
import json
import os
import sys
import sqlite3
import glob
import statistics
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

# 添加项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

# eBay 费用常量 — 单一真相源在 PricingEngine。这里转 float 是因为本页大量
# 用 pandas / Streamlit 计算，全用 Decimal 反而会到处类型错配。
from src.services.pricing_engine import PricingEngine
EBAY_FEE_RATE = float(PricingEngine.EBAY_FEE_RATE)
AD_RATE = float(PricingEngine.AD_RATE)
FIXED_FEE = float(PricingEngine.FIXED_FEE)
STORE_DISCOUNT_RATE = float(PricingEngine.STORE_DISCOUNT_RATE)

DEFAULT_DELIST_MIN_AGE_DAYS = 60

# ═══════════════════ Category Mappings ═══════════════════
# 共享词典：src/services/category_catalog.py
# (旧版 inline 字典里有 100411/63108 重复键 → 已修复)
from src.services.category_catalog import CATEGORY_KEYWORDS, CATEGORY_NAMES


# ═══════════════════ Helper Functions ═══════════════════

def make_thumbnail_html(url, size=50):
    """生成缩略图HTML"""
    if not url:
        return ''
    return f'<img src="{url}" width="{size}" height="{size}" style="object-fit:cover;border-radius:4px;" />'


def df_with_thumbnails(df, image_col='image_url', thumb_col='图片', size=50):
    """给DataFrame添加缩略图列（用于st.markdown HTML表格）"""
    if image_col in df.columns:
        df[thumb_col] = df[image_col].apply(lambda x: make_thumbnail_html(x, size))
    return df


def render_html_table(df, columns=None, max_rows=200):
    """渲染带缩略图的HTML表格"""
    if columns:
        df = df[columns]
    
    html = '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
    # Header
    html += '<tr>'
    for col in df.columns:
        html += f'<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;background:#f8f8f8;">{col}</th>'
    html += '</tr>'
    # Rows
    for _, row in df.head(max_rows).iterrows():
        html += '<tr>'
        for col in df.columns:
            val = row[col]
            align = 'center' if col == '图片' else 'left'
            html += f'<td style="padding:4px 8px;border-bottom:1px solid #eee;text-align:{align};">{val}</td>'
        html += '</tr>'
    html += '</table>'
    return html


def calc_net_margin(selling_price, total_cost):
    """计算扣除eBay费用后的净利润率 (考虑5%店铺折扣)"""
    if selling_price <= 0 or total_cost <= 0:
        return 0
    # 买家实付 = listing_price × (1 - 5%折扣)
    actual_revenue = selling_price * (1 - STORE_DISCOUNT_RATE)
    ebay_fee = actual_revenue * EBAY_FEE_RATE
    ad_fee = actual_revenue * AD_RATE
    net_profit = actual_revenue - ebay_fee - ad_fee - FIXED_FEE - total_cost
    return net_profit / selling_price


def fmt_margin(x):
    """格式化利润率，负值红色显示"""
    if not isinstance(x, (int, float)):
        return x
    txt = f"{x:.1%}"
    if x < 0:
        return f'<span style="color:red;font-weight:bold">{txt} ⚠️</span>'
    if x < 0.05:
        return f'<span style="color:orange">{txt}</span>'
    return txt


def fmt_price_with_discount(x, discount_pct):
    """格式化售价，折扣时显示折后标记"""
    if not isinstance(x, (int, float)):
        return x
    if discount_pct > 0:
        return f'${x:.0f} <small style="color:#888">(-{discount_pct:.0f}%)</small>'
    return f'${x:.0f}'


def calc_listing_age_days(published_at, created_at):
    """计算链接上架天数"""
    try:
        dt_str = published_at or created_at
        if dt_str:
            dt = datetime.fromisoformat(dt_str.split('+')[0].replace('Z', ''))
            return max(1, (datetime.now() - dt).days)
    except:
        pass
    return 30


def calc_health_score(net_margin, selling_price, market_median, age_days, total_listings,
                      impressions=0, views=0, transactions=0, sold_qty=0):
    """
    健康分数 (0-100)
    - 净利润率 30%
    - 价格竞争力 20%
    - 刊登时长 10%
    - 市场竞争 10%
    - 实际表现 30% (展示/浏览/销售)
    """
    # 1. Margin (30)
    if net_margin >= 0.20:
        m = 30
    elif net_margin >= 0.15:
        m = 26
    elif net_margin >= 0.10:
        m = 20
    elif net_margin >= 0.05:
        m = 12
    else:
        m = 0

    # 2. Competitiveness (20)
    if market_median > 0 and selling_price > 0:
        ratio = selling_price / market_median
        if ratio <= 0.85:
            c = 20
        elif ratio <= 0.95:
            c = 17
        elif ratio <= 1.05:
            c = 14
        elif ratio <= 1.15:
            c = 9
        elif ratio <= 1.30:
            c = 4
        else:
            c = 0
    else:
        c = 10

    # 3. Age (10)
    if age_days <= 7:
        a = 10
    elif age_days <= 14:
        a = 9
    elif age_days <= 21:
        a = 7
    elif age_days <= 30:
        a = 5
    elif age_days <= 45:
        a = 3
    elif age_days <= 60:
        a = 1
    else:
        a = 0

    # 4. Market competition (10)
    if total_listings == 0:
        mk = 5
    elif total_listings < 500:
        mk = 10
    elif total_listings < 2000:
        mk = 8
    elif total_listings < 5000:
        mk = 5
    elif total_listings < 10000:
        mk = 2
    else:
        mk = 0

    # 5. Performance (30) — impressions, views, transactions/sales
    perf = 0
    # 5a. Impressions (10)
    if impressions >= 1000:
        perf += 10
    elif impressions >= 500:
        perf += 8
    elif impressions >= 200:
        perf += 6
    elif impressions >= 50:
        perf += 3
    elif impressions > 0:
        perf += 1
    
    # 5b. Views/Clicks (8)
    if views >= 50:
        perf += 8
    elif views >= 20:
        perf += 6
    elif views >= 10:
        perf += 4
    elif views >= 3:
        perf += 2
    elif views > 0:
        perf += 1
    
    # 5c. Actual sales — strongest signal (12)
    total_sold = max(transactions, sold_qty)
    if total_sold >= 5:
        perf += 12
    elif total_sold >= 3:
        perf += 10
    elif total_sold >= 2:
        perf += 8
    elif total_sold >= 1:
        perf += 6
    # 0 sold = 0 bonus

    return m + c + a + mk + perf, {
        'margin': m, 'competitiveness': c, 'age': a, 'market': mk, 'performance': perf
    }


def load_latest_report():
    """加载最新的竞争分析报告"""
    reports = sorted(glob.glob(str(PROJECT_ROOT / '_competition_analysis_*.json')))
    if not reports:
        return None
    latest = reports[-1]
    with open(latest, encoding='utf-8') as f:
        return json.load(f)


def load_products_from_db():
    """从数据库加载已发布产品数据"""
    db_path = str(PROJECT_ROOT / 'ebay_collection.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT sku, title, price, shipping, suggested_price, cost_breakdown, optimization,
               status, listing_id, published_at, created_at, images
        FROM collected_products WHERE status = 'PUBLISHED'
    """)
    
    products = []
    for row in cur.fetchall():
        cost_data = json.loads(row['cost_breakdown']) if row['cost_breakdown'] else {}
        opt = json.loads(row['optimization']) if row['optimization'] else {}
        
        total_cost = cost_data.get('total_dajian_cost', 0)
        selling_price = row['suggested_price'] or 0
        cat_id = opt.get('categoryId', '?')
        
        # 提取第一张图片URL作为缩略图
        image_url = ''
        try:
            images_raw = row['images']
            if images_raw:
                img_list = json.loads(images_raw) if isinstance(images_raw, str) else images_raw
                if isinstance(img_list, list) and img_list:
                    image_url = img_list[0]
        except:
            pass
        
        products.append({
            'sku': row['sku'],
            'title': (opt.get('title') or row['title'] or '')[:60],
            'categoryId': cat_id,
            'cat_name': CATEGORY_NAMES.get(cat_id, cat_id),
            'total_cost': total_cost,
            'selling_price': selling_price,
            'net_margin': calc_net_margin(selling_price, total_cost),
            'age_days': calc_listing_age_days(row['published_at'], row['created_at']),
            'listing_id': row['listing_id'],
            'published_at': row['published_at'],
            'image_url': image_url,
            # Performance defaults (updated by merge_performance_into_products)
            'impressions': 0,
            'views': 0,
            'ctr': 0,
            'conversion_rate': 0,
            'transactions': 0,
            'sold_qty': 0,
            'revenue': 0,
            'order_count': 0,
            'has_sales': False,
            'traffic_has_record': False,
            'traffic_coverage_limited': False,
            'traffic_data_status': '未加载',
            'confirmed_no_traffic': False,
            'suspected_no_traffic': False,
        })
    
    conn.close()
    return products


def get_market_data_from_report(report):
    """从报告中提取市场数据，优先竞争分析报告，回退到 reprice 报告"""
    if report and 'market_data' in report:
        return report['market_data']
    
    # 回退: 从最新的 reprice 报告中提取 market_research
    reprice_reports = sorted(glob.glob(str(PROJECT_ROOT / 'reports' / 'reprice_report_*.json')))
    if reprice_reports:
        try:
            with open(reprice_reports[-1], encoding='utf-8') as f:
                reprice_data = json.load(f)
            mr = reprice_data.get('market_research', {})
            if mr:
                logging.info(f"Using market data from reprice report: {reprice_reports[-1]} ({len(mr)} categories)")
                return mr
        except Exception as e:
            logging.warning(f"Failed to load reprice market data: {e}")
    
    return {}


def load_performance_data(force_refresh=False):
    """
    加载 eBay 性能数据 (展示/点击/销售)
    优先使用缓存，超过4小时或强制刷新时重新获取
    
    Returns:
        dict: {traffic: {listing_id: {...}}, sales: {by_listing: {...}, by_sku: {...}}, ...}
    """
    try:
        from src.services.ebay_performance import (
            EbayPerformanceService, load_performance_cache, save_performance_cache
        )
        
        if not force_refresh:
            cached = load_performance_cache(max_age_hours=4)
            if cached:
                return cached
        
        svc = EbayPerformanceService()
        data = svc.fetch_all_performance(traffic_days=30, sales_days=90)
        save_performance_cache(data)
        return data
    except Exception as e:
        logging.warning(f"Failed to load performance data: {e}")
        return None


def merge_performance_into_products(products, perf_data):
    """
    将性能数据合并到产品列表中
    
    通过 listing_id 匹配 traffic 数据
    通过 listing_id/SKU 匹配 sales 数据
    """
    if not perf_data:
        return products
    
    traffic = perf_data.get('traffic', {})
    traffic_meta = perf_data.get('traffic_meta', {}) or {}
    traffic_coverage_limited = bool(traffic_meta.get('is_truncated'))
    traffic_api_ok = bool(traffic_meta.get('api_ok'))
    sales = perf_data.get('sales', {})
    by_listing = sales.get('by_listing', {})
    by_sku = sales.get('by_sku', {})
    
    for p in products:
        lid = p.get('listing_id', '')
        sku = p.get('sku', '')
        
        # Traffic data (by listing_id)
        has_traffic_record = bool(lid and lid in traffic)
        t = traffic.get(lid, {}) if has_traffic_record else {}
        p['impressions'] = t.get('impressions', 0)
        p['views'] = t.get('views', 0)
        p['ctr'] = t.get('ctr', 0)
        p['conversion_rate'] = t.get('conversion_rate', 0)
        p['transactions'] = t.get('transactions', 0)
        p['traffic_has_record'] = has_traffic_record
        p['traffic_coverage_limited'] = traffic_coverage_limited

        if has_traffic_record:
            if p['impressions'] == 0 and p['views'] == 0 and p['transactions'] == 0:
                p['traffic_data_status'] = '确认零流量'
                p['confirmed_no_traffic'] = True
            else:
                p['traffic_data_status'] = '有流量'
                p['confirmed_no_traffic'] = False
            p['suspected_no_traffic'] = False
        elif traffic_coverage_limited:
            # eBay Analytics only returned the top slice; missing rows are not proof of true zero.
            p['traffic_data_status'] = '流量未覆盖'
            p['confirmed_no_traffic'] = False
            p['suspected_no_traffic'] = True
        elif traffic_api_ok:
            p['traffic_data_status'] = '确认零流量'
            p['confirmed_no_traffic'] = True
            p['suspected_no_traffic'] = False
        else:
            p['traffic_data_status'] = '未加载'
            p['confirmed_no_traffic'] = False
            p['suspected_no_traffic'] = False
        
        # Sales data (try by listing_id first, then by SKU)
        s = by_listing.get(lid, {})
        if not s:
            s = by_sku.get(sku, {})
        p['sold_qty'] = s.get('qty', 0)
        p['revenue'] = s.get('revenue', 0)
        p['order_count'] = s.get('orders', 0)
        
        # Safety flag: has sales record
        p['has_sales'] = (p['sold_qty'] > 0) or (p['transactions'] > 0)
    
    return products


def build_delist_suggestions(
    products,
    min_age_days=DEFAULT_DELIST_MIN_AGE_DAYS,
    include_uncovered=True,
    excluded_skus=None,
    max_count=None,
):
    """Return old, no-sale, no-traffic delist candidates sorted by listing age desc."""
    excluded_skus = set(excluded_skus or set())
    candidates = []

    for product in products:
        sku = product.get('sku')
        if sku in excluded_skus:
            continue
        if product.get('has_sales', False):
            continue

        try:
            age_days = int(product.get('age_days') or 0)
        except (TypeError, ValueError):
            age_days = 0
        if age_days < int(min_age_days):
            continue

        confirmed_zero = bool(product.get('confirmed_no_traffic'))
        suspected_zero = bool(product.get('suspected_no_traffic')) and bool(include_uncovered)
        if not (confirmed_zero or suspected_zero):
            continue

        candidates.append(product)

    candidates = sorted(
        candidates,
        key=lambda p: (
            -int(p.get('age_days') or 0),
            0 if p.get('confirmed_no_traffic') else 1,
            float(p.get('health_score') or 999),
            str(p.get('sku') or ''),
        ),
    )
    if max_count is not None:
        return candidates[:max(0, int(max_count))]
    return candidates


def withdraw_offer_api(sku):
    """通过API下架一个SKU"""
    try:
        from src.services.ebay_auth import EbayOAuthService
        from src.clients.real_ebay_client import RealEbayClient
        from src.services.ebay_policy_manager import EbayPolicyManager
        
        oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
        policy_manager = EbayPolicyManager(oauth)
        client = RealEbayClient(oauth, policy_manager)
        
        offers = client.get_offers_by_sku(sku)
        if not offers:
            return False, "No offer found"
        
        for offer in offers:
            if offer.get('status') == 'PUBLISHED':
                ok = client.withdraw_offer(offer['offerId'])
                if ok:
                    return True, f"Withdrawn offer {offer['offerId']}"
                else:
                    return False, f"Failed to withdraw {offer['offerId']}"
        
        return False, "No published offers"
    except Exception as e:
        return False, str(e)


def update_db_status(sku, new_status='DELISTED'):
    """更新数据库中产品状态。

    DELISTED 时同时写入 MI 屏蔽名单 (TTL 30 天)，避免 Market Intelligence
    Tab 1「自动发掘爆品」立刻把刚下架的 SKU 又推荐回来。失败仅警告，
    不阻塞下架主流程。
    """
    db_path = str(PROJECT_ROOT / 'ebay_collection.db')
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("UPDATE collected_products SET status = ? WHERE sku = ?", (new_status, sku))
    conn.commit()
    conn.close()

    if str(new_status).upper() == 'DELISTED':
        try:
            from src.plugins.terapeak_research.blacklist import auto_blacklist_audit_failures
            auto_blacklist_audit_failures(
                [sku],
                reason="competition_monitor_delist",
                ttl_days=30,
                project_root=PROJECT_ROOT,
            )
        except Exception as e:
            logging.warning(f"[delist→blacklist] 写入 MI 屏蔽名单失败 (SKU={sku}): {e}")



# ═══════════════════ Main Render Function ═══════════════════

def render_competition_monitor():
    """渲染竞争监控页面"""
    st.title("📈 竞争监控 Dashboard")
    st.caption("实时监控链接健康状况 · 品类竞争分析 · 低效链接管理 · 展示/点击/销售追踪")
    
    # ─── 同步在售链接状态 (eBay ↔ 本地DB) ───
    sync_col1, sync_col2, sync_col3 = st.columns([3, 1, 1])
    with sync_col3:
        force_sync = st.button("🔄 同步在售状态", key="sync_listing_status",
                               help="从 eBay API 获取真实在售链接，自动将已结束的标记为 ENDED")
    
    sync_result = None
    if force_sync:
        with st.spinner("正在从 eBay 同步在售链接状态..."):
            try:
                from src.services.listing_status_sync import sync_listing_status
                sync_result = sync_listing_status(force_refresh=True)
                stale = sync_result.get('stale_ended', [])
                if stale:
                    st.success(f"✅ 同步完成: eBay在售 {sync_result['ebay_active_count']} 个, "
                              f"本次标记 {len(stale)} 个为 ENDED")
                else:
                    st.success(f"✅ 同步完成: eBay在售 {sync_result['ebay_active_count']} 个, 本地数据已是最新")
            except Exception as e:
                st.warning(f"同步失败: {e} — 使用本地数据")
    else:
        # 显示缓存的同步状态
        try:
            from src.services.listing_status_sync import _load_sync_cache
            cached_sync = _load_sync_cache(max_age_hours=24)  # 显示最近24h的同步信息
            if cached_sync:
                synced_at = cached_sync.get('synced_at', '')[:19]
                with sync_col1:
                    st.caption(f"🔗 在售状态同步于: {synced_at} | "
                              f"eBay在售: {cached_sync.get('ebay_active_count', '?')} | "
                              f"点击「同步在售状态」刷新")
        except:
            pass
    
    # ─── 店铺折扣管理 ───
    from src.services.ebay_discount_service import (
        EbayDiscountService, load_discount_state, get_active_discount_pct,
        calc_discounted_price, calc_net_margin_with_discount
    )

    discount_state = load_discount_state()
    active_discount = discount_state.get('active_discount')
    active_discount_pct = float(active_discount['discount_pct']) if active_discount else 0.0

    with st.expander("🏷️ 店铺促销管理 (Markdown Sale)" + (f" — 当前 {active_discount_pct}% OFF ✅" if active_discount_pct > 0 else ""), expanded=False):
        disc_col1, disc_col2, disc_col3, disc_col4 = st.columns([1.5, 1, 1, 1.5])

        if active_discount:
            # 显示当前活跃折扣
            with disc_col1:
                st.success(f"🏷️ **{active_discount.get('name', '促销')}** — **{active_discount_pct}% OFF**")
                st.caption(f"📅 {active_discount.get('start_date', '?')[:16]} → {active_discount.get('end_date', '?')[:16]}")
            with disc_col2:
                st.metric("折扣率", f"{active_discount_pct}%")
            with disc_col3:
                st.metric("链接范围", str(active_discount.get('listing_count', '全部')))
            with disc_col4:
                if st.button("🔴 关闭促销", key="stop_discount"):
                    promo_id = active_discount.get('promotion_id', '')
                    if promo_id:
                        svc = EbayDiscountService()
                        result = svc.delete_promotion(promo_id)
                        if result['success']:
                            st.success("✅ 促销已关闭")
                            st.rerun()
                        else:
                            st.error(f"关闭失败: {result.get('error', '')[:100]}")
                    else:
                        # 无 promotion_id，仅清除本地状态
                        svc = EbayDiscountService()
                        svc._clear_discount_state()
                        st.success("✅ 本地折扣状态已清除")
                        st.rerun()
        else:
            # 创建新促销
            with disc_col1:
                from src.utils.store_profile import get_store_profile
                promo_name = st.text_input("促销名称", value=f"{get_store_profile().brand_name} Sale {datetime.now().strftime('%m/%d')}",
                                           key="promo_name")
            with disc_col2:
                disc_pct = st.number_input("折扣%", min_value=1.0, max_value=30.0,
                                            value=5.0, step=1.0, key="disc_pct")
            with disc_col3:
                disc_days = st.number_input("持续天数", min_value=1, max_value=30,
                                             value=2, step=1, key="disc_days")
            with disc_col4:
                if st.button("🟢 开启全店促销", key="start_discount", type="primary"):
                    svc = EbayDiscountService()
                    start = datetime.now(timezone.utc)
                    end = start + timedelta(days=disc_days)
                    result = svc.create_markdown_sale(
                        name=promo_name,
                        discount_pct=disc_pct,
                        listing_ids=None,
                        start_date=start,
                        end_date=end,
                        auto_select_all=True,
                    )
                    if result.get('success'):
                        mode = result.get('mode', '')
                        if mode == 'synced_from_ebay':
                            st.success(f"✅ 已同步 eBay 运行中的促销活动到本地")
                            st.info("💡 eBay Marketing API 不支持通过 API 创建新促销。"
                                   "已自动同步 Seller Hub 中运行的促销到本地 Dashboard。")
                        elif mode == 'local_only':
                            st.success(f"✅ 已在本地设置 {disc_pct}% 折扣率")
                            st.info("💡 eBay Marketing API 不支持通过 API 创建新促销。\n\n"
                                   "请在 **Seller Hub → Marketing → Promotions** 手动创建 Markdown Sale，"
                                   "本地折扣率已设置，Dashboard 价格计算将自动反映折扣。")
                        else:
                            st.success(f"✅ 已开启 {disc_pct}% 全店促销，持续 {disc_days} 天")
                        st.rerun()
                    else:
                        err_msg = result.get('error', '')
                        st.error(f"❌ 开启失败: {err_msg[:200]}")
                        st.info("💡 请在 **Seller Hub → Marketing → Promotions** 手动创建促销，"
                               "然后点击下方按钮同步或手动设置折扣率。")
                        btn_col1, btn_col2 = st.columns(2)
                        with btn_col1:
                            if st.button("🔄 同步 eBay 促销", key="sync_promo"):
                                synced = svc.sync_running_promotions()
                                if synced:
                                    st.success(f"✅ 已同步: {synced.get('name', '促销')}")
                                    st.rerun()
                                else:
                                    st.warning("eBay 上没有运行中的 Markdown Sale")
                        with btn_col2:
                            if st.button("📝 手动设置本地折扣率", key="manual_disc"):
                                svc._save_discount_state({
                                    'discount_pct': disc_pct,
                                    'promotion_id': 'LOCAL',
                                    'name': promo_name,
                                    'start_date': start.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                                    'end_date': end.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                                    'listing_count': 'ALL',
                                })
                                st.success(f"✅ 已设置本地 {disc_pct}% 折扣率")
                                st.rerun()

        # 折扣对利润的影响预览
        if active_discount_pct > 0:
            st.markdown("---")
            st.caption(f"⚠️ 所有表格中的 **售价** 和 **利润率** 已按 **{active_discount_pct}% OFF** 折后价重新计算。红色利润表示亏本。")

    # 加载数据 (同步后重新读取，确保 ENDED 的已被过滤)
    products = load_products_from_db()
    report = load_latest_report()
    market_data = get_market_data_from_report(report)
    
    if not products:
        st.warning("没有已发布的产品数据")
        return
    
    # ─── 加载性能数据 (展示/点击/销售) ───
    perf_col1, perf_col2 = st.columns([4, 1])
    with perf_col2:
        force_refresh = st.button("🔄 刷新性能数据", key="refresh_perf")
    
    perf_data = load_performance_data(force_refresh=force_refresh)
    
    if perf_data:
        products = merge_performance_into_products(products, perf_data)
        fetched_at = perf_data.get('fetched_at', '')
        with perf_col1:
            st.caption(f"eBay 性能数据更新于: {fetched_at[:19] if fetched_at else 'N/A'} "
                       f"| 流量: 近{perf_data.get('traffic_days', 30)}天 | 订单: 近{perf_data.get('sales_days', 90)}天")
    else:
        with perf_col1:
            st.caption("⚠️ 性能数据未加载 (点击「刷新性能数据」获取展示/点击/销售指标)")
    
    # ─── 折扣价格同步: 所有产品重新计算 selling_price / net_margin ───
    if active_discount_pct > 0:
        for p in products:
            orig_price = p['selling_price']
            disc_price = round(orig_price * (1 - active_discount_pct / 100), 2)
            p['original_price'] = orig_price
            p['selling_price'] = disc_price  # 全部替换为折后价
            p['net_margin'] = calc_net_margin_with_discount(
                orig_price, p['total_cost'], active_discount_pct
            )
    else:
        for p in products:
            p['original_price'] = p['selling_price']

    # 计算健康分数 (含性能数据)
    for p in products:
        mkt = market_data.get(p['categoryId'], {})
        market_median = mkt.get('median', 0)
        total_listings = mkt.get('total_listings', 0)
        score, breakdown = calc_health_score(
            p['net_margin'], p['selling_price'], market_median, p['age_days'], total_listings,
            impressions=p.get('impressions', 0),
            views=p.get('views', 0),
            transactions=p.get('transactions', 0),
            sold_qty=p.get('sold_qty', 0),
        )
        p['health_score'] = score
        p['score_breakdown'] = breakdown
        p['market_median'] = market_median
        p['total_listings'] = total_listings
    
    df = pd.DataFrame(products)
    
    # ═══════════════════ KPI 卡片 ═══════════════════
    st.markdown("---")
    kpi1, kpi2, kpi3, kpi4, kpi5, kpi6, kpi7 = st.columns(7)
    
    avg_margin = df['net_margin'].mean()
    avg_score = df['health_score'].mean()
    low_margin_count = len(df[df['net_margin'] < 0.05])
    high_comp_cats = len(set(df[df['total_listings'] > 5000]['categoryId'])) if 'total_listings' in df.columns and df['total_listings'].any() else 0
    bottom_10_count = max(1, int(len(df) * 0.10))
    
    total_impressions = int(df['impressions'].sum()) if 'impressions' in df.columns else 0
    total_views = int(df['views'].sum()) if 'views' in df.columns else 0
    total_sold = int(df['sold_qty'].sum()) if 'sold_qty' in df.columns else 0
    has_sales_count = int(df['has_sales'].sum()) if 'has_sales' in df.columns else 0
    total_revenue = df['revenue'].sum() if 'revenue' in df.columns else 0
    
    # 优先显示 eBay API 报告的在售总数，delta 显示本工具管理的数量
    try:
        from src.services.listing_status_sync import _load_sync_cache
        _sc = _load_sync_cache(max_age_hours=24)
        _ebay_total = _sc.get('ebay_active_count', 0) if _sc else 0
    except:
        _ebay_total = 0
    if _ebay_total > 0:
        kpi1.metric("eBay在售", f"{_ebay_total}", delta=f"工具管理 {len(df)}")
    else:
        kpi1.metric("总在售", f"{len(df)}")
    kpi2.metric("平均健康分", f"{avg_score:.0f}/100")
    kpi3.metric("总展示量", f"{total_impressions:,}")
    kpi4.metric("总浏览量", f"{total_views:,}")
    kpi5.metric("售出数量", f"{total_sold}", delta=f"${total_revenue:,.0f}" if total_revenue > 0 else None)
    kpi6.metric("有销售记录", f"{has_sales_count} 🛡️")
    kpi7.metric("低利润(<5%)", f"{low_margin_count}", delta=f"-{low_margin_count}" if low_margin_count > 0 else "0", delta_color="inverse")
    
    # ═══════════════════ Tabs ═══════════════════
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
        "🎯 转化率诊断",
        "🏷️ 品类竞争概览",
        "📊 性能追踪",
        "📉 链接健康排行",
        "🗑️ 下架管理",
        "📋 详细分析",
        "📢 广告监控",
        "🆕 品类机会",
    ])

    # ═══════════════════ Tab 1: 转化率诊断 (CRO) ═══════════════════
    with tab1:
        render_cro_tab(products, market_data)

    # ═══════════════════ Tab 2: 品类竞争概览 ═══════════════════
    with tab2:
        st.subheader("品类竞争 × 利润率矩阵")
        st.caption("气泡大小 = 产品数量 | 颜色 = 平均利润率 | X轴 = 竞品数量")
        
        # 按品类汇总
        cat_summary = []
        for cat_id in df['categoryId'].unique():
            cat_sub = df[df['categoryId'] == cat_id]
            mkt = market_data.get(cat_id, {})
            cat_impressions = int(cat_sub['impressions'].sum()) if 'impressions' in cat_sub.columns else 0
            cat_views = int(cat_sub['views'].sum()) if 'views' in cat_sub.columns else 0
            cat_sold = int(cat_sub['sold_qty'].sum()) if 'sold_qty' in cat_sub.columns else 0
            cat_revenue = float(cat_sub['revenue'].sum()) if 'revenue' in cat_sub.columns else 0
            cat_summary.append({
                'cat_id': cat_id,
                'cat_name': CATEGORY_NAMES.get(cat_id, cat_id),
                'count': len(cat_sub),
                'avg_margin': cat_sub['net_margin'].mean(),
                'avg_price': cat_sub['selling_price'].mean(),
                'market_median': mkt.get('median', 0),
                'total_listings': mkt.get('total_listings', 0),
                'avg_score': cat_sub['health_score'].mean(),
                'impressions': cat_impressions,
                'views': cat_views,
                'sold': cat_sold,
                'revenue': cat_revenue,
            })
        
        cat_df = pd.DataFrame(cat_summary)
        
        # 散点图: X=竞品数(log), Y=利润率, size=产品数
        if not cat_df.empty:
            try:
                import plotly.express as px
                import plotly.graph_objects as go
                import numpy as np
                
                # 对竞品数量取 log 避免所有气泡堆在一起
                cat_df['listings_display'] = cat_df['total_listings'].apply(
                    lambda x: max(x, 1))  # 避免 log(0)

                fig = px.scatter(
                    cat_df,
                    x='listings_display',
                    y='avg_margin',
                    size='count',
                    color='avg_score',
                    text='cat_name',
                    hover_data={
                        'listings_display': False,
                        'total_listings': ':,',
                        'avg_price': ':.2f',
                        'market_median': ':.2f',
                        'count': True,
                        'avg_margin': ':.1%',
                        'avg_score': ':.1f',
                    },
                    color_continuous_scale='RdYlGn',
                    labels={
                        'listings_display': '市场竞品数量',
                        'avg_margin': '平均净利润率',
                        'count': '我们的产品数',
                        'avg_score': '健康分',
                        'avg_price': '均售价',
                        'market_median': '市场中位价',
                        'total_listings': '竞品数(精确)',
                    },
                    title='品类竞争力矩阵',
                    size_max=55,
                )
                fig.update_traces(
                    textposition='top center',
                    textfont_size=10,
                    textfont_color='#333',
                )
                fig.update_layout(
                    height=600,
                    xaxis=dict(
                        type='log',
                        title='市场竞品数量 (对数刻度)',
                        tickvals=[1, 10, 100, 1000, 10000, 100000],
                        ticktext=['1', '10', '100', '1K', '10K', '100K'],
                        gridcolor='#eee',
                    ),
                    yaxis=dict(
                        title='平均净利润率',
                        tickformat='.0%',
                        gridcolor='#eee',
                    ),
                    plot_bgcolor='white',
                )
                # 添加危险区域标注
                fig.add_hline(y=0.10, line_dash="dash", line_color="red",
                              annotation_text="10% 利润率警戒线",
                              annotation_position="bottom right")
                fig.add_vline(x=5000, line_dash="dash", line_color="orange",
                              annotation_text="高竞争线",
                              annotation_position="top left")
                st.plotly_chart(fig, use_container_width=True)
            except ImportError:
                st.info("安装 plotly 以查看交互图表: pip install plotly")
        
        # 表格: 危险品类
        st.subheader("⚠️ 高竞争 + 低利润品类")
        danger_cats = cat_df[(cat_df['total_listings'] > 2000) & (cat_df['avg_margin'] < 0.10)]
        if not danger_cats.empty:
            danger_display = danger_cats[['cat_name', 'count', 'avg_margin', 'total_listings', 'avg_price', 'market_median', 'impressions', 'sold', 'avg_score']].copy()
            danger_display.columns = ['品类', '产品数', '利润率', '竞品数', '均售价', '市场中位价', '展示', '售出', '健康分']
            danger_display['利润率'] = danger_display['利润率'].apply(fmt_margin)
            danger_display['竞品数'] = danger_display['竞品数'].apply(lambda x: f"{x:,.0f}")
            danger_display['均售价'] = danger_display['均售价'].apply(lambda x: f"${x:.0f}")
            danger_display['市场中位价'] = danger_display['市场中位价'].apply(lambda x: f"${x:.0f}")
            danger_display['展示'] = danger_display['展示'].apply(lambda x: f"{int(x):,}")
            danger_display['健康分'] = danger_display['健康分'].apply(lambda x: f"{x:.0f}")
            st.dataframe(danger_display, use_container_width=True, hide_index=True)
        else:
            st.success("没有需要特别关注的品类")
        
        # 全品类表
        st.subheader("📋 全品类概览")
        cat_df_sorted = cat_df.sort_values('total_listings', ascending=False)
        all_cat_display = cat_df_sorted[['cat_name', 'count', 'avg_margin', 'total_listings', 'avg_price', 'market_median', 'impressions', 'views', 'sold', 'avg_score']].copy()
        all_cat_display.columns = ['品类', '产品数', '利润率', '竞品数', '均售价', '市场中位价', '展示', '浏览', '售出', '健康分']
        all_cat_display['利润率'] = all_cat_display['利润率'].apply(fmt_margin)
        all_cat_display['竞品数'] = all_cat_display['竞品数'].apply(lambda x: f"{x:,.0f}")
        all_cat_display['均售价'] = all_cat_display['均售价'].apply(lambda x: f"${x:.0f}")
        all_cat_display['市场中位价'] = all_cat_display['市场中位价'].apply(lambda x: f"${x:.0f}")
        all_cat_display['展示'] = all_cat_display['展示'].apply(lambda x: f"{int(x):,}")
        all_cat_display['健康分'] = all_cat_display['健康分'].apply(lambda x: f"{x:.0f}")
        st.dataframe(all_cat_display, use_container_width=True, hide_index=True)
    
    # ═══════════════════ Tab 3: 性能追踪 ═══════════════════
    with tab3:
        st.subheader("📊 展示 · 浏览 · 销售 性能追踪")
        
        if 'impressions' not in df.columns or df['impressions'].sum() == 0:
            st.info("暂无性能数据。点击页面顶部「🔄 刷新性能数据」获取 eBay 展示/点击/销售数据。")
        else:
            # ── Performance Overview ──
            st.caption("⚠️ eBay Analytics API 限制：仅返回展示量前 200 名链接的数据，其余链接展示量显示为 0（非真实0，是API无法获取）")
            
            st.markdown("#### 🔥 热门链接 (按展示量排序)")
            perf_df = df.sort_values('impressions', ascending=False).head(30).copy()
            
            try:
                import plotly.express as px
                
                fig = px.bar(
                    perf_df.head(20),
                    x='sku',
                    y='impressions',
                    color='views',
                    text='views',
                    hover_data=['title', 'cat_name', 'sold_qty', 'selling_price'],
                    color_continuous_scale='Blues',
                    labels={
                        'impressions': '展示量',
                        'views': '浏览量',
                        'sku': 'SKU',
                    },
                    title='Top 20 展示量 (颜色深度 = 浏览量)',
                )
                fig.update_traces(textposition='outside', textfont_size=9)
                fig.update_layout(height=450, xaxis_tickangle=-45)
                st.plotly_chart(fig, use_container_width=True)
            except ImportError:
                pass
            
            # ── Sales highlight ──
            st.markdown("#### 💰 有销售记录的链接")
            sold_df = df[df['has_sales'] == True].sort_values('sold_qty', ascending=False).copy()
            
            if not sold_df.empty:
                st.success(f"共 {len(sold_df)} 个链接有销售记录，总售出 {int(sold_df['sold_qty'].sum())} 件，"
                          f"总销售额 ${sold_df['revenue'].sum():,.2f}")
                
                sold_display = sold_df[['image_url', 'sku', 'title', 'cat_name', 'sold_qty', 'revenue',
                                         'impressions', 'views', 'health_score', 'selling_price', 'net_margin', 'age_days']].copy()
                sold_display = df_with_thumbnails(sold_display)
                sold_display = sold_display.drop(columns=['image_url'])
                sold_display.columns = ['SKU', '标题', '品类', '售出', '销售额',
                                         '展示', '浏览', '健康分', '售价', '利润率', '天数', '图片']
                # Reorder with thumbnail first
                sold_display = sold_display[['图片', 'SKU', '标题', '品类', '售出', '销售额',
                                              '展示', '浏览', '健康分', '售价', '利润率', '天数']]
                sold_display['销售额'] = sold_display['销售额'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) else x)
                sold_display['售价'] = sold_display['售价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) else x)
                sold_display['利润率'] = sold_display['利润率'].apply(fmt_margin)
                sold_display['展示'] = sold_display['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
                
                st.markdown(render_html_table(sold_display), unsafe_allow_html=True)
            else:
                st.info("近期暂无销售记录")
            
            # ── Zero performance listings ──
            st.markdown("#### ⚠️ 零展示 / 零浏览链接")
            zero_imp = df[df['impressions'] == 0]
            zero_views = df[(df['impressions'] > 0) & (df['views'] == 0)]
            
            zcol1, zcol2 = st.columns(2)
            zcol1.metric("零展示", f"{len(zero_imp)} 个链接")
            zcol2.metric("有展示但零浏览", f"{len(zero_views)} 个链接")
            
            if not zero_imp.empty:
                with st.expander(f"查看 {len(zero_imp)} 个零展示链接"):
                    zero_display = zero_imp[['image_url', 'sku', 'title', 'cat_name', 'selling_price', 'net_margin', 'age_days', 'health_score']].copy()
                    zero_display = zero_display.sort_values('health_score')
                    zero_display = df_with_thumbnails(zero_display)
                    zero_display = zero_display.drop(columns=['image_url'])
                    zero_display.columns = ['SKU', '标题', '品类', '售价', '利润率', '天数', '健康分', '图片']
                    zero_display = zero_display[['图片', 'SKU', '标题', '品类', '售价', '利润率', '天数', '健康分']]
                    zero_display['售价'] = zero_display['售价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) else x)
                    zero_display['利润率'] = zero_display['利润率'].apply(fmt_margin)
                    st.markdown(render_html_table(zero_display), unsafe_allow_html=True)
            
            # ── Full performance table ──
            st.markdown("#### 📋 全部链接性能数据")
            full_perf = df.sort_values('impressions', ascending=False).copy()
            full_display = full_perf[['image_url', 'sku', 'title', 'cat_name', 'impressions', 'views', 
                                       'ctr', 'sold_qty', 'revenue', 'health_score', 
                                       'selling_price', 'net_margin', 'age_days']].copy()
            full_display = df_with_thumbnails(full_display)
            full_display = full_display.drop(columns=['image_url'])
            full_display.columns = ['SKU', '标题', '品类', '展示', '浏览', 
                                     'CTR', '售出', '销售额', '健康分', '售价', '利润率', '天数', '图片']
            full_display = full_display[['图片', 'SKU', '标题', '品类', '展示', '浏览', 
                                          'CTR', '售出', '销售额', '健康分', '售价', '利润率', '天数']]
            full_display['CTR'] = full_display['CTR'].apply(lambda x: f"{x:.1%}" if isinstance(x, (int, float)) and x > 0 else "-")
            full_display['销售额'] = full_display['销售额'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) and x > 0 else "-")
            full_display['售价'] = full_display['售价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) else x)
            full_display['利润率'] = full_display['利润率'].apply(fmt_margin)
            full_display['展示'] = full_display['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
            st.markdown(render_html_table(full_display), unsafe_allow_html=True)
    
    # ═══════════════════ Tab 4: 链接健康排行 ═══════════════════
    with tab4:
        st.subheader("链接健康分数排行")
        
        # Score distribution histogram
        try:
            import plotly.express as px
            fig = px.histogram(
                df, x='health_score', nbins=20, 
                color_discrete_sequence=['#636EFA'],
                labels={'health_score': '健康分数', 'count': '数量'},
                title='健康分数分布'
            )
            fig.add_vline(x=30, line_dash="dash", line_color="red", annotation_text="危险线 (30分)")
            fig.update_layout(height=350)
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            pass
        
        # Filters
        col1, col2, col3 = st.columns(3)
        with col1:
            score_filter = st.slider("健康分数上限", 0, 100, 40, 5, key="score_filter")
        with col2:
            margin_filter = st.slider("最大利润率", 0.0, 0.30, 0.10, 0.01, key="margin_filter", format="%.0f%%")
        with col3:
            sort_by = st.selectbox("排序", [
                '健康分(低→高)', '利润率(低→高)', '上架天数(久→新)', 
                '售价(高→低)', '展示量(低→高)', '售出(多→少)'
            ], key="sort_by")
        
        # Filter and sort
        filtered = df[(df['health_score'] <= score_filter) | (df['net_margin'] <= margin_filter)].copy()
        
        sort_map = {
            '健康分(低→高)': ('health_score', True),
            '利润率(低→高)': ('net_margin', True),
            '上架天数(久→新)': ('age_days', False),
            '售价(高→低)': ('selling_price', False),
            '展示量(低→高)': ('impressions', True),
            '售出(多→少)': ('sold_qty', False),
        }
        sort_col, sort_asc = sort_map.get(sort_by, ('health_score', True))
        filtered = filtered.sort_values(sort_col, ascending=sort_asc)
        
        st.info(f"符合筛选条件的链接: {len(filtered)} / {len(df)}")
        
        # Display table with performance data
        if not filtered.empty:
            display_cols = ['image_url', 'sku', 'title', 'cat_name', 'health_score', 'net_margin', 
                            'selling_price', 'market_median', 'impressions', 'views', 
                            'sold_qty', 'total_listings', 'age_days']
            # Filter to existing columns only
            display_cols = [c for c in display_cols if c in filtered.columns]
            display_df = filtered[display_cols].copy()
            
            display_df = df_with_thumbnails(display_df)
            if 'image_url' in display_df.columns:
                display_df = display_df.drop(columns=['image_url'])
            
            col_names = {
                'sku': 'SKU', 'title': '标题', 'cat_name': '品类', 
                'health_score': '健康分', 'net_margin': '利润率',
                'selling_price': '售价', 'market_median': '市场价',
                'impressions': '展示', 'views': '浏览', 'sold_qty': '售出',
                'total_listings': '竞品数', 'age_days': '天数',
            }
            display_df.columns = [col_names.get(c, c) for c in display_df.columns]
            
            # Move thumbnail to front
            if '图片' in display_df.columns:
                cols = ['图片'] + [c for c in display_df.columns if c != '图片']
                display_df = display_df[cols]
            
            if '利润率' in display_df.columns:
                display_df['利润率'] = display_df['利润率'].apply(fmt_margin)
            if '售价' in display_df.columns:
                display_df['售价'] = display_df['售价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) else x)
            if '市场价' in display_df.columns:
                display_df['市场价'] = display_df['市场价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) and x > 0 else "N/A")
            if '竞品数' in display_df.columns:
                display_df['竞品数'] = display_df['竞品数'].apply(lambda x: f"{x:,.0f}" if isinstance(x, (int, float)) and x > 0 else "N/A")
            if '展示' in display_df.columns:
                display_df['展示'] = display_df['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
            if '浏览' in display_df.columns:
                display_df['浏览'] = display_df['浏览'].apply(lambda x: int(x) if isinstance(x, (int, float)) else x)
            
            st.markdown(render_html_table(display_df), unsafe_allow_html=True)
        else:
            st.success("没有符合筛选条件的低分链接")
    
    # ═══════════════════ Tab 5: 下架管理 ═══════════════════
    with tab5:
        st.subheader("🗑️ 批量下架管理")
        st.warning("此操作将从eBay下架选中的链接")
        
        # Initialize excluded SKUs in session state
        if 'delist_excluded_skus' not in st.session_state:
            st.session_state.delist_excluded_skus = set()
        
        # Safety notice
        sales_protected = [p for p in products if p.get('has_sales', False)]
        if sales_protected:
            st.info(f"🛡️ **销售保护**: {len(sales_protected)} 个链接有销售记录，已自动排除在建议下架列表外")

        traffic_meta = (perf_data or {}).get('traffic_meta', {}) if perf_data else {}
        traffic_coverage_limited = bool(traffic_meta.get('is_truncated'))
        if traffic_coverage_limited:
            st.warning(
                "eBay Analytics 当前只返回展示量前 "
                f"{traffic_meta.get('limit', 200)} 条；未返回的链接会标记为“流量未覆盖”，"
                "不能等同于已确认真实 0 流量。"
            )
        
        filter_col1, filter_col2, filter_col3 = st.columns([1.2, 1.6, 3])
        with filter_col1:
            min_age_days = int(st.number_input(
                "最小刊登天数",
                min_value=1,
                max_value=365,
                value=DEFAULT_DELIST_MIN_AGE_DAYS,
                step=5,
                key="delist_min_age_days",
            ))
        with filter_col2:
            include_uncovered = st.checkbox(
                "包含流量未覆盖",
                value=True,
                key="delist_include_uncovered",
                help="eBay 流量接口截断时，未返回的老链接只能视为疑似无流量；关闭后只显示已确认 0 流量的链接。",
            )
        with filter_col3:
            st.caption("建议逻辑：无销售保护 + 刊登时间足够长 + 无流量信号；列表按刊登天数从高到低。")

        # 老死链自动建议 — EXCLUDE listings with sales
        sorted_products = sorted(products, key=lambda x: (-int(x.get('age_days') or 0), x.get('health_score', 999)))
        bottom_count = max(1, int(len(sorted_products) * 0.10))
        all_matching_suggestions = build_delist_suggestions(
            products,
            min_age_days=min_age_days,
            include_uncovered=include_uncovered,
            excluded_skus=st.session_state.delist_excluded_skus,
        )
        
        # Cap the default suggestion set to keep accidental bulk actions bounded.
        suggested_products = all_matching_suggestions[:bottom_count]
        confirmed_suggestions = [p for p in all_matching_suggestions if p.get('confirmed_no_traffic')]
        uncovered_suggestions = [p for p in all_matching_suggestions if p.get('suspected_no_traffic')]
        
        st.markdown(
            f"**自动建议下架:** 刊登 ≥ {min_age_days} 天 + 无销售 + "
            f"{'确认零流量/流量未覆盖' if include_uncovered else '确认零流量'} "
            f"= {len(all_matching_suggestions)} 个链接"
        )
        st.caption(
            f"确认零流量 {len(confirmed_suggestions)} 个；"
            f"流量未覆盖待人工复核 {len(uncovered_suggestions)} 个；"
            f"默认最多显示最老的 {bottom_count} 个。"
        )
        if st.session_state.delist_excluded_skus:
            st.markdown(f"🚫 已排除 {len(st.session_state.delist_excluded_skus)} 个SKU")
        
        # ── Per-row table with delist/exclude buttons ──
        if suggested_products:
            st.markdown(f"### 建议下架列表 ({len(suggested_products)} 个)")
            
            for idx, p in enumerate(suggested_products):
                sku = p['sku']
                cols = st.columns([0.8, 1.5, 3.2, 1.4, 0.9, 1.3, 0.8, 0.9, 1.1, 1.2, 1.2])
                
                # Thumbnail
                thumb_url = p.get('image_url', '')
                if thumb_url:
                    cols[0].image(thumb_url, width=50)
                else:
                    cols[0].write("📦")
                
                cols[1].write(f"**{sku}**")
                title = p.get('title', '')[:40]
                cols[2].write(title)
                cols[3].write(f"🏷️ {p.get('cat_name', 'N/A')}")
                cols[4].write(f"📅 {int(p.get('age_days') or 0)}天")
                cols[5].write(f"📡 {p.get('traffic_data_status', '未加载')}")
                cols[6].write(f"⭐ {p['health_score']}")
                cols[7].write(f"💰 {p['net_margin']:.1%}")
                
                imp = p.get('impressions', 0)
                sold = p.get('sold_qty', 0)
                cols[8].write(f"👁️ {int(imp):,} / 🛒 {sold}")
                
                # Delist button
                if cols[9].button("🗑️ 下架", key=f"delist_single_{idx}_{sku}"):
                    with st.spinner(f"正在下架 {sku}..."):
                        ok, msg = withdraw_offer_api(sku)
                        if ok:
                            update_db_status(sku, 'DELISTED')
                            st.success(f"✅ {sku} 已下架")
                            time.sleep(1)
                            st.rerun()
                        else:
                            st.error(f"❌ {sku}: {msg}")
                
                # Exclude button
                if cols[10].button("🚫 排除", key=f"delist_exclude_{idx}_{sku}"):
                    st.session_state.delist_excluded_skus.add(sku)
                    st.rerun()
            
            st.divider()
        else:
            st.success("当前没有符合“老链接 + 无销售 + 无流量信号”的建议下架候选")
        
        # ── Batch multiselect (existing flow) ──
        with st.expander("📋 批量选择下架", expanded=False):
            def _format_delist_option(p):
                sales_flag = " 🛡️" if p.get('has_sales', False) else ""
                traffic_flag = p.get('traffic_data_status', '未加载')
                return (
                    f"{p['sku']} | {p['cat_name']} | {int(p.get('age_days') or 0)}天 "
                    f"| {traffic_flag} | 分:{p['health_score']} | {p['net_margin']:.1%}"
                    f"{sales_flag}"
                )

            all_sku_options = []
            for p in sorted_products:
                if p['sku'] in st.session_state.delist_excluded_skus:
                    continue
                all_sku_options.append(_format_delist_option(p))
            
            default_selection = [_format_delist_option(p) for p in suggested_products]
            
            selected = st.multiselect(
                "选择要下架的链接",
                options=all_sku_options,
                default=default_selection,
                key="delist_selection"
            )
            
            selected_skus = [s.split(' | ')[0] for s in selected]
            
            if selected_skus:
                st.write(f"已选择 {len(selected_skus)} 个链接")
                
                # Safety check before delist
                sales_in_selection = [s for s in selected_skus if any(p['sku'] == s and p.get('has_sales', False) for p in products)]
                if sales_in_selection:
                    st.error(f"⚠️ 以下链接有销售记录，已阻止批量下架: {', '.join(sales_in_selection)}")
                
                # Confirm & execute
                confirm = st.text_input("输入 'DELIST' 确认批量下架", key="delist_confirm")
                if st.button(
                    "⚡ 执行批量下架",
                    type="primary",
                    disabled=(confirm != "DELIST" or bool(sales_in_selection)),
                    key="exec_delist",
                ):
                    progress = st.progress(0)
                    status_text = st.empty()
                    success_count = 0
                    fail_count = 0
                    
                    for i, sku in enumerate(selected_skus):
                        status_text.text(f"下架 {sku} ({i+1}/{len(selected_skus)})...")
                        ok, msg = withdraw_offer_api(sku)
                        if ok:
                            update_db_status(sku, 'DELISTED')
                            success_count += 1
                        else:
                            fail_count += 1
                            st.warning(f"⚠️ {sku}: {msg}")
                        progress.progress((i + 1) / len(selected_skus))
                        time.sleep(0.5)
                    
                    status_text.empty()
                    st.success(f"✅ 下架完成! 成功: {success_count}, 失败: {fail_count}")
                    st.rerun()
        
        # ── Reset excluded list ──
        if st.session_state.delist_excluded_skus:
            if st.button("🔄 重置排除列表", key="reset_excluded"):
                st.session_state.delist_excluded_skus = set()
                st.rerun()
    
    # ═══════════════════ Tab 6: 详细分析 ═══════════════════
    with tab6:
        st.subheader("📊 单品类深入分析")
        
        # Category selector
        cat_options = sorted(df['categoryId'].unique(), key=lambda x: CATEGORY_NAMES.get(x, x) or str(x))
        cat_labels = {c: f"{CATEGORY_NAMES.get(c, c) or c} ({len(df[df['categoryId'] == c])}个)" for c in cat_options}
        
        selected_cat = st.selectbox(
            "选择品类",
            options=cat_options,
            format_func=lambda x: cat_labels.get(x, str(x)) or str(x),
            key="cat_detail_select"
        )
        
        if selected_cat:
            cat_products = df[df['categoryId'] == selected_cat].sort_values('health_score')
            mkt = market_data.get(selected_cat, {})
            
            # Category summary with performance
            col1, col2, col3, col4, col5, col6 = st.columns(6)
            col1.metric("产品数", len(cat_products))
            col2.metric("平均利润率", f"{cat_products['net_margin'].mean():.1%}")
            col3.metric("市场中位价", f"${mkt.get('median', 0):.0f}")
            col4.metric("竞品数量", f"{mkt.get('total_listings', 0):,}")
            cat_impressions = int(cat_products['impressions'].sum()) if 'impressions' in cat_products.columns else 0
            cat_sold = int(cat_products['sold_qty'].sum()) if 'sold_qty' in cat_products.columns else 0
            col5.metric("总展示", f"{cat_impressions:,}")
            col6.metric("总售出", f"{cat_sold}")
            
            # Market comparison
            st.markdown("---")
            st.markdown("**我们的价格 vs 市场价格**")
            
            try:
                import plotly.graph_objects as go
                
                fig = go.Figure()
                fig.add_trace(go.Bar(
                    name='我们的售价',
                    x=cat_products['sku'],
                    y=cat_products['selling_price'],
                    marker_color='#636EFA'
                ))
                if mkt.get('median', 0) > 0:
                    fig.add_hline(y=mkt['median'], line_dash="dash", line_color="red",
                                  annotation_text=f"市场中位价 ${mkt['median']:.0f}")
                if mkt.get('p25', 0) > 0:
                    fig.add_hline(y=mkt['p25'], line_dash="dot", line_color="orange",
                                  annotation_text=f"P25 ${mkt['p25']:.0f}")
                fig.update_layout(height=400, xaxis_tickangle=-45, title="SKU 售价 vs 市场水位")
                st.plotly_chart(fig, use_container_width=True)
            except ImportError:
                pass
            
            # Product table with performance data
            st.markdown("**该品类所有链接:**")
            cat_cols = ['image_url', 'sku', 'title', 'health_score', 'net_margin', 
                        'selling_price', 'market_median', 'impressions', 'views',
                        'sold_qty', 'age_days']
            cat_cols = [c for c in cat_cols if c in cat_products.columns]
            cat_display = cat_products[cat_cols].copy()
            
            cat_display = df_with_thumbnails(cat_display)
            if 'image_url' in cat_display.columns:
                cat_display = cat_display.drop(columns=['image_url'])
            
            col_renames = {
                'sku': 'SKU', 'title': '标题', 'health_score': '健康分',
                'net_margin': '利润率', 'selling_price': '售价', 'market_median': '市场价',
                'impressions': '展示', 'views': '浏览', 'sold_qty': '售出', 'age_days': '天数',
            }
            cat_display.columns = [col_renames.get(c, c) for c in cat_display.columns]
            
            # Move thumbnail to front
            if '图片' in cat_display.columns:
                cols = ['图片'] + [c for c in cat_display.columns if c != '图片']
                cat_display = cat_display[cols]
            
            if '利润率' in cat_display.columns:
                cat_display['利润率'] = cat_display['利润率'].apply(fmt_margin)
            if '售价' in cat_display.columns:
                cat_display['售价'] = cat_display['售价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) else x)
            if '市场价' in cat_display.columns:
                cat_display['市场价'] = cat_display['市场价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) and x > 0 else "N/A")
            if '展示' in cat_display.columns:
                cat_display['展示'] = cat_display['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
            if '浏览' in cat_display.columns:
                cat_display['浏览'] = cat_display['浏览'].apply(lambda x: int(x) if isinstance(x, (int, float)) else x)
            st.markdown(render_html_table(cat_display), unsafe_allow_html=True)
        
        # Report metadata
        st.markdown("---")
        st.markdown("**数据来源**")
        if report:
            ts = report.get('timestamp', 'Unknown')
            st.caption(f"最近报告: {ts}")
            st.caption("运行 `python _competition_analysis.py` 更新市场数据")
        else:
            st.warning("未找到竞争分析报告。请先运行: `python _competition_analysis.py`")

    # ═══════════════════ Tab 7: 广告监控 ═══════════════════
    with tab7:
        render_ad_monitor_tab(df, products, perf_data)

    # ═══════════════════ Tab 8: 品类机会 ═══════════════════
    with tab8:
        render_category_opportunity_tab(df, market_data, report)


# ─── Tab 6 Implementation: 广告监控 ───

def render_ad_monitor_tab(df, products, perf_data):
    """渲染广告监控标签页"""
    st.subheader("📢 广告监控 & 建议")
    st.caption("基于 Promoted Listings Standard (PLS) — 仅成交时扣费。利润率必须 > 竞价率+2% 才建议推广。")
    st.caption("口径说明: 本页利润率 = 净利润 / 折后成交价；推广中链接会按真实 bid% 计入广告费。智能定价报告里的利润率 = 净利润 / 扣费后净收，所以 9.1% 净收利润通常约等于 7.4% 成交价利润。")

    # Load ad data
    from src.services.ebay_ad_service import (
        load_ad_cache, generate_ad_recommendations, EbayAdService, save_ad_cache
    )

    ad_col1, ad_col2 = st.columns([4, 1])
    with ad_col2:
        refresh_ad = st.button("🔄 刷新广告数据", key="refresh_ad")

    ad_data = load_ad_cache(force_refresh=refresh_ad)

    if not ad_data:
        st.warning("广告数据未加载。点击「🔄 刷新广告数据」获取。")
        return

    with ad_col1:
        fetched = ad_data.get('fetched_at', '')[:19]
        st.caption(f"广告数据更新于: {fetched}")

    ads = ad_data.get('ads', {})
    # Normalize ads to dict keyed by listing_id
    if isinstance(ads, list):
        ads = {a['listing_id']: a for a in ads if 'listing_id' in a}
    campaigns = ad_data.get('campaigns', [])

    # Generate recommendations
    recs = generate_ad_recommendations(products, ad_data, perf_data)

    # ── KPI Cards ──
    promoted_count = sum(1 for r in recs if r['has_ad'])
    not_promoted = sum(1 for r in recs if not r['has_ad'])
    total_est_ad_cost = sum(r['est_ad_cost'] for r in recs)
    total_ad_revenue = sum(r['revenue'] for r in recs if r['has_ad'])
    total_ad_sold = sum(r['sold_qty'] for r in recs if r['has_ad'])
    overall_roas = total_ad_revenue / total_est_ad_cost if total_est_ad_cost > 0 else 0

    stop_count = sum(1 for r in recs if r['action'] == 'STOP_AD')
    start_count = sum(1 for r in recs if r['action'] == 'START_AD')

    kc1, kc2, kc3, kc4, kc5, kc6 = st.columns(6)
    kc1.metric("推广中", f"{promoted_count}", delta=f"共{len(recs)}个链接")
    kc2.metric("未推广", f"{not_promoted}")
    kc3.metric("广告成交", f"{total_ad_sold} 件", delta=f"${total_ad_revenue:,.0f}" if total_ad_revenue > 0 else None)
    kc4.metric("估计广告费", f"${total_est_ad_cost:,.2f}")
    kc5.metric("ROAS", f"{overall_roas:.1f}x" if overall_roas > 0 else "N/A")
    kc6.metric("建议调整", f"🔴{stop_count} 🟢{start_count}")

    st.markdown("---")

    # ── Campaign Overview ──
    st.markdown("#### 📋 广告活动概览")
    active_campaigns = [c for c in campaigns if c.get('ad_count', 0) > 0]
    if active_campaigns:
        camp_data = []
        for c in active_campaigns:
            camp_ads = [r for r in recs if r['has_ad'] and r.get('campaign_name') == c['name']]
            camp_imp = sum(r['impressions'] for r in camp_ads)
            camp_sold = sum(r['sold_qty'] for r in camp_ads)
            camp_rev = sum(r['revenue'] for r in camp_ads)
            camp_data.append({
                '活动名称': c['name'],
                '链接数': c['ad_count'],
                '竞价率': f"{c['bid_percentage']}%",
                '总展示': f"{camp_imp:,}",
                '成交': camp_sold,
                '销售额': f"${camp_rev:,.0f}",
            })
        st.dataframe(pd.DataFrame(camp_data), use_container_width=True, hide_index=True)
    else:
        st.info("暂无有链接的活跃广告活动")

    # ── Recommendation Sections ──
    st.markdown("---")

    # Helper: campaign selector for adding ads
    active_campaigns = [c for c in campaigns if c.get('ad_count', 0) > 0]
    all_campaigns = campaigns  # for dropdown

    # ── 🔴 建议关闭广告 ──
    stop_recs = [r for r in recs if r['action'] == 'STOP_AD']
    if stop_recs:
        st.markdown(f"#### 🔴 建议关闭广告 ({len(stop_recs)} 个)")
        st.error("这些链接利润不足以覆盖广告费，或表现极差，建议立即关闭")

        # Batch close button
        stop_col1, stop_col2 = st.columns([3, 1])
        with stop_col2:
            if st.button(f"⚡ 一键关闭全部 ({len(stop_recs)}个)", key="batch_stop", type="primary"):
                svc = EbayAdService()
                ok_count = 0
                fail_count = 0
                progress = st.progress(0)
                for i, r in enumerate(stop_recs):
                    cid = r.get('campaign_id', '')
                    if not cid:
                        ad_info_lookup = ads.get(r['listing_id'], {})
                        cid = ad_info_lookup.get('campaign_id', '')
                    if cid:
                        result = svc.delete_ad(cid, r['listing_id'])
                        if result['success']:
                            ok_count += 1
                        else:
                            fail_count += 1
                    progress.progress((i + 1) / len(stop_recs))
                st.success(f"✅ 已关闭 {ok_count} 个广告" + (f"，{fail_count} 个失败" if fail_count else ""))
                # Refresh cache
                try:
                    fresh = svc.fetch_all_ad_data()
                    save_ad_cache(fresh)
                except:
                    pass

        stop_df = pd.DataFrame(stop_recs)
        stop_display = stop_df[['image_url', 'sku', 'title', 'campaign_name', 'bid_percentage',
                                 'impressions', 'views', 'sold_qty', 'net_margin', 'age_days', 'reason']].copy()
        stop_display = df_with_thumbnails(stop_display)
        stop_display = stop_display.drop(columns=['image_url'])
        stop_display.columns = ['SKU', '标题', '活动', '竞价%', '展示', '浏览', '售出', '利润率', '天数', '原因', '图片']
        stop_display = stop_display[['图片', 'SKU', '标题', '活动', '竞价%', '展示', '浏览', '售出', '利润率', '天数', '原因']]
        stop_display['利润率'] = stop_display['利润率'].apply(fmt_margin)
        stop_display['展示'] = stop_display['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
        stop_display['竞价%'] = stop_display['竞价%'].apply(lambda x: f"{x}%" if isinstance(x, (int, float)) else x)
        st.markdown(render_html_table(stop_display), unsafe_allow_html=True)

        # Individual close buttons
        with st.expander("🔧 逐个关闭广告"):
            for r in stop_recs:
                sc1, sc2, sc3 = st.columns([3, 1, 1])
                sc1.write(f"**{r['sku']}** — {r['title'][:40]} (利润{r['net_margin']:.1%})")
                cid_r = r.get('campaign_id', '')
                if not cid_r:
                    ad_info_r = ads.get(r['listing_id'], {})
                    cid_r = ad_info_r.get('campaign_id', '')
                if sc2.button("🔴 关闭", key=f"stop_{r['listing_id']}"):
                    if cid_r:
                        svc = EbayAdService()
                        res = svc.delete_ad(cid_r, r['listing_id'])
                        if res['success']:
                            sc3.success("✅")
                        else:
                            sc3.error(f"❌ {res.get('error','')[:50]}")
                    else:
                        sc3.warning("找不到campaign_id")

    # ── 🟢 建议开启广告 ──
    start_recs = sorted([r for r in recs if r['action'] == 'START_AD'], key=lambda x: -x['priority'])
    if start_recs:
        st.markdown(f"#### 🟢 建议开启广告 ({len(start_recs)} 个)")
        st.success("这些链接利润率 > 7%（覆盖5%广告+2%缓冲），曝光不足，适合推广")

        # Campaign selector for batch add
        start_col1, start_col2, start_col3 = st.columns([2, 2, 1])
        camp_options = {c['name']: c['id'] for c in all_campaigns if c.get('status') == 'RUNNING'}
        with start_col1:
            selected_camp = st.selectbox(
                "选择目标活动", options=list(camp_options.keys()),
                key="start_campaign_select",
                help="将链接添加到此广告活动中"
            )
        with start_col2:
            start_bid = st.number_input("竞价率%", min_value=1.0, max_value=20.0, value=5.0, step=0.5, key="start_bid")
        with start_col3:
            st.write("")  # spacer
            if st.button(f"⚡ 批量开启 ({len(start_recs)}个)", key="batch_start"):
                target_cid = camp_options.get(selected_camp, '')
                if target_cid:
                    svc = EbayAdService()
                    ok_count = 0
                    fail_count = 0
                    progress = st.progress(0)
                    for i, r in enumerate(start_recs):
                        result = svc.create_ad_safe(
                            target_cid, r['listing_id'], sku=r.get('sku'),
                            bid_percentage=start_bid,
                        )
                        if result['success']:
                            ok_count += 1
                        else:
                            fail_count += 1
                        progress.progress((i + 1) / len(start_recs))
                    st.success(f"✅ 已开启 {ok_count} 个广告" + (f"，{fail_count} 个失败" if fail_count else ""))
                    try:
                        fresh = svc.fetch_all_ad_data()
                        save_ad_cache(fresh)
                    except:
                        pass
                else:
                    st.error("请先选择目标广告活动")

        start_df = pd.DataFrame(start_recs)
        start_display = start_df[['image_url', 'sku', 'title', 'cat_name', 'selling_price',
                                    'net_margin', 'age_days', 'impressions', 'views', 'reason']].copy()
        start_display = df_with_thumbnails(start_display)
        start_display = start_display.drop(columns=['image_url'])
        start_display.columns = ['SKU', '标题', '品类', '售价', '利润率', '天数', '展示', '浏览', '原因', '图片']
        start_display = start_display[['图片', 'SKU', '标题', '品类', '售价', '利润率', '天数', '展示', '浏览', '原因']]
        start_display['售价'] = start_display['售价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) else x)
        start_display['利润率'] = start_display['利润率'].apply(fmt_margin)
        start_display['展示'] = start_display['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
        st.markdown(render_html_table(start_display), unsafe_allow_html=True)

        # Individual start buttons
        with st.expander("🔧 逐个开启广告"):
            for r in start_recs:
                sc1, sc2, sc3 = st.columns([3, 1, 1])
                sc1.write(f"**{r['sku']}** — {r['title'][:40]} (利润{r['net_margin']:.1%})")
                if sc2.button("🟢 开启", key=f"start_{r['listing_id']}"):
                    target_cid = camp_options.get(selected_camp, '')
                    if target_cid:
                        svc = EbayAdService()
                        res = svc.create_ad_safe(
                            target_cid, r['listing_id'], sku=r.get('sku'),
                            bid_percentage=start_bid,
                        )
                        if res['success']:
                            sc3.success("✅")
                        else:
                            sc3.error(f"❌ {res.get('error','')[:50]}")
                    else:
                        sc3.warning("选择活动")

    # ── 🟡 建议提高竞价 ──
    increase_recs = [r for r in recs if r['action'] == 'INCREASE_BID']
    if increase_recs:
        st.markdown(f"#### 🟡 建议提高竞价 ({len(increase_recs)} 个)")
        st.warning("这些链接利润充足但曝光不足，考虑将竞价从5%提高到7-8%")

        # ── 批量提高竞价控件 ──
        inc_col1, inc_col2, inc_col3 = st.columns([2, 2, 1])
        with inc_col1:
            new_bid = st.number_input("目标竞价率%", min_value=1.0, max_value=20.0,
                                       value=7.0, step=0.5, key="increase_bid_target",
                                       help="将选中链接的竞价率统一调整到此值")
        with inc_col2:
            st.write("")  # spacer
            st.caption(f"当前平均竞价: {sum(r['bid_percentage'] for r in increase_recs)/len(increase_recs):.1f}%")
        with inc_col3:
            st.write("")  # spacer
            if st.button(f"⚡ 批量提高 ({len(increase_recs)}个)", key="batch_increase_bid"):
                svc = EbayAdService()
                items = [{'campaign_id': r.get('campaign_id', ''), 'listing_id': r['listing_id']}
                         for r in increase_recs]
                # 补充缺失的 campaign_id
                missing_cid_count = 0
                for item, rec in zip(items, increase_recs):
                    if not item['campaign_id']:
                        ad_info = ads.get(rec['listing_id'], {})
                        item['campaign_id'] = ad_info.get('campaign_id', '')
                    if not item['campaign_id']:
                        missing_cid_count += 1

                # 过滤掉无 campaign_id 的项
                valid_items = [it for it in items if it['campaign_id']]

                if not valid_items:
                    st.error(f"❌ 全部 {len(items)} 个商品缺少 campaign_id，"
                             f"请先点击「🔄 刷新广告数据」后重试")
                else:
                    with st.spinner(f"正在批量更新 {len(valid_items)} 个竞价..."):
                        results = svc.batch_update_ad_bids(valid_items, new_bid)
                    ok_count = sum(1 for r in results if r.get('success'))
                    fail_count = sum(1 for r in results if not r.get('success'))
                    fail_count += missing_cid_count  # 加上缺 campaign_id 的

                    if ok_count > 0:
                        st.success(f"✅ 已更新 {ok_count} 个竞价到 {new_bid}%"
                                   + (f"，{fail_count} 个失败" if fail_count else ""))
                    else:
                        st.error(f"❌ 全部 {fail_count} 个更新失败")

                    # 显示失败详情
                    failed_results = [r for r in results if not r.get('success')]
                    if failed_results:
                        with st.expander(f"📋 查看 {len(failed_results)} 个失败详情"):
                            for r in failed_results[:20]:
                                st.text(f"  listing {r.get('listing_id', '?')}: {r.get('error', '未知错误')}")
                            if len(failed_results) > 20:
                                st.text(f"  ... 还有 {len(failed_results) - 20} 个失败")

                    if missing_cid_count:
                        st.warning(f"⚠️ {missing_cid_count} 个商品缺少 campaign_id，已跳过。请刷新广告数据后重试。")

                    # 刷新缓存
                    try:
                        fresh = svc.fetch_all_ad_data()
                        save_ad_cache(fresh)
                    except Exception:
                        pass

        inc_df = pd.DataFrame(increase_recs)
        inc_display = inc_df[['image_url', 'sku', 'title', 'campaign_name', 'bid_percentage',
                               'impressions', 'views', 'net_margin', 'age_days', 'reason']].copy()
        inc_display = df_with_thumbnails(inc_display)
        inc_display = inc_display.drop(columns=['image_url'])
        inc_display.columns = ['SKU', '标题', '活动', '当前竞价%', '展示', '浏览', '利润率', '天数', '原因', '图片']
        inc_display = inc_display[['图片', 'SKU', '标题', '活动', '当前竞价%', '展示', '浏览', '利润率', '天数', '原因']]
        inc_display['利润率'] = inc_display['利润率'].apply(fmt_margin)
        inc_display['展示'] = inc_display['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
        st.markdown(render_html_table(inc_display), unsafe_allow_html=True)

        # ── 逐个提高竞价按钮 ──
        with st.expander("🔧 逐个调整竞价"):
            for r in increase_recs:
                ic1, ic2, ic3, ic4 = st.columns([3, 1, 1, 1])
                ic1.write(f"**{r['sku']}** — {r['title'][:40]} (当前{r['bid_percentage']}%, 利润{r['net_margin']:.1%})")
                single_bid = ic2.number_input("竞价%", min_value=1.0, max_value=20.0,
                                               value=new_bid, step=0.5, key=f"inc_bid_{r['listing_id']}")
                if ic3.button("🟡 提高", key=f"increase_{r['listing_id']}"):
                    cid_r = r.get('campaign_id', '')
                    if not cid_r:
                        ad_info_r = ads.get(r['listing_id'], {})
                        cid_r = ad_info_r.get('campaign_id', '')
                    if cid_r:
                        svc = EbayAdService()
                        res = svc.update_ad_bid(cid_r, r['listing_id'], single_bid)
                        if res['success']:
                            ic4.success(f"✅ {single_bid}%")
                        else:
                            ic4.error(f"❌ {res.get('error','')[:50]}")
                    else:
                        ic4.warning("无campaign")

    # ✅ 广告有效 (有销售)
    keep_with_sales = [r for r in recs if r['action'] == 'KEEP_AD' and r['sold_qty'] > 0]
    if keep_with_sales:
        st.markdown(f"#### ✅ 广告有效 — 保持推广 ({len(keep_with_sales)} 个)")
        keep_df = pd.DataFrame(keep_with_sales)
        keep_display = keep_df[['image_url', 'sku', 'title', 'campaign_name', 'bid_percentage',
                                 'impressions', 'views', 'sold_qty', 'revenue', 'est_ad_cost', 'roas', 'age_days']].copy()
        keep_display = df_with_thumbnails(keep_display)
        keep_display = keep_display.drop(columns=['image_url'])
        keep_display.columns = ['SKU', '标题', '活动', '竞价%', '展示', '浏览', '售出', '销售额', '广告费', 'ROAS', '天数', '图片']
        keep_display = keep_display[['图片', 'SKU', '标题', '活动', '竞价%', '展示', '浏览', '售出', '销售额', '广告费', 'ROAS', '天数']]
        keep_display['销售额'] = keep_display['销售额'].apply(lambda x: f"${x:,.0f}" if isinstance(x, (int, float)) else x)
        keep_display['广告费'] = keep_display['广告费'].apply(lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) else x)
        keep_display['ROAS'] = keep_display['ROAS'].apply(lambda x: f"{x:.1f}x" if isinstance(x, (int, float)) and x > 0 else "-")
        keep_display['展示'] = keep_display['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
        keep_display['竞价%'] = keep_display['竞价%'].apply(lambda x: f"{x}%" if isinstance(x, (int, float)) else x)
        st.markdown(render_html_table(keep_display), unsafe_allow_html=True)

    # 📊 全部广告链接总览
    with st.expander("📊 查看全部推广链接"):
        promoted_recs = [r for r in recs if r['has_ad']]
        if promoted_recs:
            prom_df = pd.DataFrame(promoted_recs)
            prom_display = prom_df[['image_url', 'sku', 'title', 'campaign_name', 'bid_percentage',
                                     'impressions', 'views', 'ctr', 'sold_qty', 'revenue',
                                     'net_margin', 'age_days', 'action']].copy()
            prom_display = df_with_thumbnails(prom_display)
            prom_display = prom_display.drop(columns=['image_url'])
            prom_display.columns = ['SKU', '标题', '活动', '竞价%', '展示', '浏览', 'CTR',
                                     '售出', '销售额', '利润率', '天数', '建议', '图片']
            prom_display = prom_display[['图片', 'SKU', '标题', '活动', '竞价%', '展示', '浏览',
                                          'CTR', '售出', '销售额', '利润率', '天数', '建议']]
            action_map = {'KEEP_AD': '✅保持', 'STOP_AD': '🔴关闭', 'INCREASE_BID': '🟡提高', 'NO_CHANGE': '—'}
            prom_display['建议'] = prom_display['建议'].map(action_map).fillna('—')
            prom_display['CTR'] = prom_display['CTR'].apply(lambda x: f"{x:.2%}" if isinstance(x, (int, float)) else x)
            prom_display['销售额'] = prom_display['销售额'].apply(lambda x: f"${x:,.0f}" if isinstance(x, (int, float)) and x > 0 else "-")
            prom_display['利润率'] = prom_display['利润率'].apply(fmt_margin)
            prom_display['展示'] = prom_display['展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
            prom_display['竞价%'] = prom_display['竞价%'].apply(lambda x: f"{x}%" if isinstance(x, (int, float)) else x)
            st.markdown(render_html_table(prom_display), unsafe_allow_html=True)
        else:
            st.info("暂无推广中的链接")

    # 广告预算说明
    st.markdown("---")
    st.caption("💡 **PLS (Promoted Listings Standard)** 仅在成交时按竞价率扣费，无成交不收费。"
               f" 当前默认竞价率 5%，估计广告费 = 售价 × 5% × 成交量。"
               f" ROAS (广告回报率) = 销售额 ÷ 广告费。")


# ─── Tab 7 Implementation: 品类机会 ───

def render_category_opportunity_tab(df, market_data, report):
    """渲染品类机会建议标签页"""
    st.subheader("🆕 未刊登品类 & 高潜力机会")
    st.caption("发现未被覆盖的高利润、低竞争、高需求品类，指导选品方向")

    # 已刊登的品类
    listed_categories = set(df['categoryId'].unique()) if 'categoryId' in df.columns else set()
    listed_cat_names = {}
    for _, row in df.iterrows():
        listed_cat_names[row.get('categoryId', '')] = row.get('cat_name', '')

    # 品类数据 (from market_data)
    if not market_data:
        st.warning("没有市场数据。请先运行 `python _competition_analysis.py` 获取竞争数据。")
        return

    # ── 已刊登品类表现热力图 ──
    st.markdown("#### 📊 已刊登品类表现矩阵")

    cat_stats = []
    for cat_id, mkt in market_data.items():
        cat_products = df[df['categoryId'] == cat_id] if 'categoryId' in df.columns else pd.DataFrame()
        if cat_products.empty:
            continue

        count = len(cat_products)
        avg_margin = cat_products['net_margin'].mean() if 'net_margin' in cat_products.columns else 0
        total_listings = mkt.get('total_listings', 0)
        avg_imp = cat_products['impressions'].mean() if 'impressions' in cat_products.columns else 0
        total_sold = cat_products['sold_qty'].sum() if 'sold_qty' in cat_products.columns else 0
        est_str = mkt.get('estimated_str', 0)
        cat_name = listed_cat_names.get(cat_id, CATEGORY_NAMES.get(cat_id, cat_id))

        cat_stats.append({
            'cat_id': cat_id,
            'cat_name': cat_name,
            'count': count,
            'avg_margin': avg_margin,
            'total_listings': total_listings,
            'avg_impressions': avg_imp,
            'total_sold': total_sold,
            'est_str': est_str,
            'median_price': mkt.get('median', 0),
        })

    if cat_stats:
        cat_stats_df = pd.DataFrame(cat_stats)
        cat_stats_df = cat_stats_df.sort_values('total_sold', ascending=False)

        display = cat_stats_df[['cat_name', 'count', 'avg_margin', 'total_listings',
                                 'avg_impressions', 'total_sold', 'est_str', 'median_price']].copy()
        display.columns = ['品类', '我的链接', '平均利润', '竞品数', '平均展示', '总售出', '预估STR%', '市场中位价']
        display['平均利润'] = display['平均利润'].apply(lambda x: f"{x:.1%}" if isinstance(x, (int, float)) else x)
        display['竞品数'] = display['竞品数'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) and x > 0 else "N/A")
        display['平均展示'] = display['平均展示'].apply(lambda x: f"{int(x):,}" if isinstance(x, (int, float)) else x)
        display['预估STR%'] = display['预估STR%'].apply(lambda x: f"{x:.1f}%" if isinstance(x, (int, float)) and x > 0 else "N/A")
        display['市场中位价'] = display['市场中位价'].apply(lambda x: f"${x:.0f}" if isinstance(x, (int, float)) and x > 0 else "N/A")
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.info("暂无品类数据")

    # ── 库存中可立即上架的高潜力 SKU (与 MI Tab 1 共享 IntelligenceService) ──
    st.markdown("---")
    st.markdown("#### 🎯 库存中的高潜力 SKU (待上架)")
    st.caption(
        "调用 `IntelligenceService.auto_discover_opportunities()` — "
        "与 Market Intelligence「自动发掘爆品」同源；这里只看**未刊登**的 SKU，"
        "并按已上架品类聚合，告诉你哪个品类还能加链接。"
    )

    opp_col1, opp_col2, opp_col3 = st.columns([1, 1, 2])
    with opp_col1:
        opp_min_margin = st.slider(
            "最低利润率", 0.10, 0.40, 0.20, 0.05,
            key="t7_opp_min_margin",
        )
    with opp_col2:
        opp_max = st.slider(
            "扫描数量", 10, 100, 30, 10,
            key="t7_opp_max",
        )
    with opp_col3:
        st.write("")
        run_opp = st.button(
            "🚀 扫描库存机会", type="primary",
            use_container_width=True, key="t7_run_opp",
        )

    if run_opp:
        with st.spinner("调用 IntelligenceService.auto_discover_opportunities …"):
            try:
                from src.plugins.terapeak_research.intelligence_service import IntelligenceService
                opportunities = IntelligenceService().auto_discover_opportunities(
                    min_margin=opp_min_margin,
                    max_results=opp_max,
                )
            except Exception as e:
                st.error(f"调用失败: {e}")
                opportunities = []

        if not opportunities:
            st.info("没有发现高潜力机会。可放宽利润率或先在 MI 页面运行一次完整扫描。")
        else:
            # 只看未刊登的 (PENDING / READY)，已 PUBLISHED 的让 MI 自己处理
            unlisted = [o for o in opportunities if (o.get('status') or '').upper() in ('PENDING', 'READY')]
            with_real_str = sum(1 for o in opportunities if o.get('seller_str_pct') is not None)

            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("发现机会", len(opportunities))
            mc2.metric("待上架 (PENDING/READY)", len(unlisted))
            mc3.metric("带实测 STR", with_real_str, help="来自竞争监控性能缓存")
            mc4.metric(
                "潜在总利润",
                f"${sum(o.get('potential_profit', 0) for o in opportunities):,.0f}",
            )

            # 按品类聚合：哪个品类最值得加链接
            by_cat: dict = {}
            for o in opportunities:
                kw = (o.get('search_keywords') or '').lower()
                # 没有 categoryId（IntelligenceService 不返回它），先按搜索词当 bucket
                bucket = by_cat.setdefault(kw or '(未知)', {'count': 0, 'profit': 0, 'skus': []})
                bucket['count'] += 1
                bucket['profit'] += o.get('potential_profit', 0)
                bucket['skus'].append(o.get('sku', ''))
            cat_rows = sorted(
                ({'搜索词': k, '机会数': v['count'], '潜在利润': v['profit'],
                  '示例 SKU': ', '.join(v['skus'][:3])} for k, v in by_cat.items()),
                key=lambda r: -r['潜在利润'],
            )
            if cat_rows:
                st.markdown("**按搜索词聚合 (告诉你哪个细分品类还能加链接)：**")
                cat_rows_df = pd.DataFrame(cat_rows[:15]).copy()
                cat_rows_df['潜在利润'] = cat_rows_df['潜在利润'].apply(lambda x: f"${x:,.0f}")
                st.dataframe(cat_rows_df, use_container_width=True, hide_index=True)

            # 待上架 SKU 详表
            if unlisted:
                st.markdown(f"**📋 待上架的 {len(unlisted)} 个 SKU (按机会分排序)：**")
                rows = []
                for o in unlisted:
                    rows.append({
                        '图': o.get('image_url') or '',
                        'SKU': o.get('sku', ''),
                        '标题': (o.get('title') or '')[:60],
                        '状态': o.get('status', ''),
                        '机会分': o.get('opportunity_score', 0),
                        '需求(估)': o.get('demand_signal_score', 0),
                        '实测STR%': (
                            f"{o['seller_str_pct']:.1f}"
                            if o.get('seller_str_pct') is not None else '—'
                        ),
                        '成本$': o.get('dajian_cost', 0),
                        '建议价$': o.get('suggested_price', 0),
                        '利润$': o.get('potential_profit', 0),
                        '利润率%': o.get('margin_rate', 0),
                        '竞争': o.get('competition', ''),
                        '大建链接': o.get('url', ''),
                    })
                df_unlisted = pd.DataFrame(rows)
                try:
                    st.dataframe(
                        df_unlisted, use_container_width=True, hide_index=True,
                        column_config={
                            '图': st.column_config.ImageColumn('图', width='small'),
                            '大建链接': st.column_config.LinkColumn('大建', width='small'),
                        },
                    )
                except Exception:
                    st.dataframe(df_unlisted, use_container_width=True, hide_index=True)

                ready_skus = [o['sku'] for o in unlisted if (o.get('status') or '').upper() == 'READY']
                if ready_skus:
                    st.markdown("**🚀 直接走标准 audit + publish 流程：**")
                    st.code(
                        "python scripts/audit_fix_ready_drafts.py\n"
                        + "\n".join(f"python batch_publish.py --sku {s}" for s in ready_skus[:10])
                        + ("\n# ...还有更多" if len(ready_skus) > 10 else ""),
                        language="bash",
                    )

            with st.expander("👉 想看完整 KPI、历史趋势、批量审计+发布？"):
                st.markdown(
                    "前往 **🎯 Market Intelligence → Tab 1「自动发掘爆品」**，"
                    "提供 14d KPI 走势、sparkline、批量发布封装等完整能力。"
                    "本 Tab 只是把同一引擎接入「竞争监控」上下文，便于你在监控同时快速决策补货。"
                )

    # 使用建议
    st.markdown("---")
    st.info(
        "💡 **选品决策**:\n"
        "1. 上面「待上架的 SKU」表里 **机会分≥70 + 利润率≥20%** 的优先发布\n"
        "2. 标了「实测 STR」的 SKU 是用我们已刊同类的真实数据，比 Browse 估算更可信\n"
        "3. 如果某个搜索词出现 ≥5 个机会，说明这个细分赛道我们还有空间加链接\n"
        "4. 想看完整爆品发掘 / 关键词分析 / 趋势发掘 → 切到 Market Intelligence 页面"
    )



# ─── Tab 1 Implementation: 转化率诊断 (CRO) ─────────────────────────

def _cro_delist_base_url():
    from src.utils.store_profile import get_store_profile
    return os.environ.get('CRO_DELIST_BASE_URL', get_store_profile().server_base_url)


def _load_cro_delist_confirmation_rows(limit=500):
    try:
        from scripts.cro_delist import load_confirmation_rows
        return load_confirmation_rows(
            base_url=_cro_delist_base_url(),
            db_path=PROJECT_ROOT / 'ebay_collection.db',
            limit=limit,
        )
    except Exception as exc:
        logging.warning(f"[CRO delist] load confirmation rows failed: {exc}")
        return []


def _render_cro_delist_confirmation_panel(limit=500):
    rows = _load_cro_delist_confirmation_rows(limit=limit)
    st.markdown("#### 🗑️ 待人工确认下架")
    last_batch = st.session_state.get('cro_batch_delist_last_result')
    if last_batch:
        msg = (
            f"上次批量下架: 成功 {last_batch.get('ok', 0)} · "
            f"失败 {last_batch.get('failed', 0)} · 跳过 {last_batch.get('skipped', 0)}"
        )
        if last_batch.get('failed') or last_batch.get('skipped'):
            st.warning(msg)
        else:
            st.success(msg)
        with st.expander("查看上次批量结果", expanded=False):
            try:
                st.dataframe(
                    pd.DataFrame(last_batch.get('results', [])),
                    use_container_width=True,
                    hide_index=True,
                )
            except Exception:
                st.json(last_batch)

    c1, c2, c3 = st.columns([1, 1, 2])
    c1.metric("待确认", len(rows))
    c2.metric("确认服务", _cro_delist_base_url().replace('http://', '').replace('https://', ''))
    with c3:
        if st.button("生成/刷新确认链接", key="cro_generate_delist_links"):
            try:
                from scripts.cro_delist import build_magic_links
                rep = build_magic_links(
                    base_url=_cro_delist_base_url(),
                    db_path=PROJECT_ROOT / 'ebay_collection.db',
                    limit=limit,
                )
                st.success(f"已生成 {len(rep.get('rows', []))} 条确认链接")
                st.rerun()
            except Exception as exc:
                st.error(f"生成确认链接失败: {exc}")

    if not rows:
        st.info("当前没有未确认的下架链接。")
        return rows

    try:
        df_links = pd.DataFrame(rows)
        nonce = int(st.session_state.get('cro_delist_batch_nonce', 0))
        select_all = st.checkbox(
            f"全选当前 {len(df_links)} 条",
            value=False,
            key=f"cro_delist_select_all_{nonce}",
        )
        display = df_links[[
            'priority', 'sku', 'status', 'reason', 'expires_at', 'url'
        ]].copy()
        display.columns = ['优先级', 'SKU', '状态', '原因', '过期时间', '确认下架']
        display.insert(0, '选择', bool(select_all))
        edited = st.data_editor(
            display,
            use_container_width=True,
            hide_index=True,
            height=420,
            disabled=['优先级', 'SKU', '状态', '原因', '过期时间', '确认下架'],
            column_config={
                '选择': st.column_config.CheckboxColumn(
                    '选择',
                    help='勾选后可批量确认下架',
                    width='small',
                ),
                '确认下架': st.column_config.LinkColumn(
                    '确认下架',
                    display_text='打开确认',
                    width='small',
                ),
            },
            key=f"cro_delist_editor_{nonce}",
        )
        selected_skus = edited.loc[edited['选择'], 'SKU'].astype(str).tolist()
        b1, b2, b3 = st.columns([1.4, 1, 1.6])
        confirmed = b1.checkbox(
            "我确认下架所选 SKU",
            value=False,
            key=f"cro_delist_bulk_confirm_{nonce}",
        )
        b2.metric("已选择", len(selected_skus))
        with b3:
            if st.button(
                f"批量确认下架 ({len(selected_skus)})",
                type='primary',
                disabled=not selected_skus or not confirmed,
                key=f"cro_delist_batch_button_{nonce}",
                use_container_width=True,
            ):
                try:
                    from scripts.cro_delist import batch_confirm_pending_delists
                    with st.spinner("正在批量下架所选 SKU..."):
                        rep = batch_confirm_pending_delists(
                            selected_skus,
                            db_path=PROJECT_ROOT / 'ebay_collection.db',
                            max_count=limit,
                        )
                    st.session_state['cro_batch_delist_last_result'] = rep
                    st.session_state['cro_delist_batch_nonce'] = nonce + 1
                    st.rerun()
                except Exception as exc:
                    st.error(f"批量下架失败: {exc}")
    except Exception:
        st.dataframe(rows, use_container_width=True)
    return rows

def render_cro_tab(products, market_data):
    """渲染 CRO 转化率诊断标签页 — 主线: 提升转化率."""
    st.subheader("🎯 转化率诊断 (Conversion Rate Optimization)")
    st.caption("竞争监控 → 漏斗分析 → 可执行动作.  低 CTR 往往是价格/标题/主图问题；低 CVR 多半是 specifics/价格/运费.")

    from src.services.conversion_diagnoser import (
        diagnose_batch, summarize, top_actions,
        HEALTHY_CTR, HEALTHY_CVR, HEALTHY_STR, MIN_IMPRESSIONS_FOR_DIAGNOSIS,
    )

    # 把 images 字段补上 (load_products_from_db 没存 list, 只存第一张 image_url)
    enriched = []
    for p in products:
        q = dict(p)
        q['images'] = [p['image_url']] if p.get('image_url') else []
        enriched.append(q)

    diagnoses = diagnose_batch(enriched, market_data=market_data)
    summary = summarize(diagnoses)

    # ─── KPI ───
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("诊断 SKU", summary['total'])
    k2.metric("平均 CRO 分", f"{summary.get('avg_cro_score', 0):.0f}/100")
    k3.metric("健康", summary.get('healthy_count', 0))
    by_stage = summary.get('by_funnel_stage', {})
    k4.metric("曝光→点击漏 (低CTR)", by_stage.get('low_ctr', 0))
    k5.metric("点击→下单漏 (低CVR)", by_stage.get('low_cvr', 0))
    k6.metric("零曝光", by_stage.get('no_impression', 0))

    st.caption(
        f"健康基线: CTR ≥ {HEALTHY_CTR*100:.1f}% · "
        f"CVR ≥ {HEALTHY_CVR*100:.1f}% · "
        f"STR ≥ {HEALTHY_STR*100:.3f}% · "
        f"展示 ≥ {MIN_IMPRESSIONS_FOR_DIAGNOSIS} 才进入诊断"
    )

    st.markdown("---")

    # ─── 趋势曲线 (Slice 7: 14d 平均 CRO 分 + 各漏斗段堆叠) ───
    try:
        import sqlite3
        import pandas as pd
        import plotly.graph_objects as go
        from datetime import date as _date, timedelta as _td
        with sqlite3.connect(str(PROJECT_ROOT / 'ebay_collection.db')) as _c:
            cur = _c.execute(
                "SELECT snapshot_date, AVG(cro_score) AS avg_score, "
                "       SUM(CASE WHEN funnel_stage='healthy' THEN 1 ELSE 0 END) AS healthy, "
                "       SUM(CASE WHEN funnel_stage='low_ctr' THEN 1 ELSE 0 END) AS low_ctr, "
                "       SUM(CASE WHEN funnel_stage='low_cvr' THEN 1 ELSE 0 END) AS low_cvr, "
                "       SUM(CASE WHEN funnel_stage='no_impression' THEN 1 ELSE 0 END) AS no_imp "
                "FROM cro_snapshots WHERE snapshot_date >= ? "
                "GROUP BY snapshot_date ORDER BY snapshot_date",
                ((_date.today() - _td(days=14)).isoformat(),)
            )
            trend_rows = cur.fetchall()
        if len(trend_rows) >= 2:
            tdf = pd.DataFrame(trend_rows, columns=[
                'date', 'avg_score', 'healthy', 'low_ctr', 'low_cvr', 'no_imp'])
            tc1, tc2 = st.columns(2)
            with tc1:
                f1 = go.Figure()
                f1.add_trace(go.Scatter(x=tdf['date'], y=tdf['avg_score'],
                                        mode='lines+markers', name='平均 CRO 分',
                                        line=dict(color='#2E86AB', width=3)))
                f1.update_layout(title='14 天平均 CRO 分趋势 (北极星指标)',
                                 yaxis=dict(range=[0, 100]), height=300,
                                 margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(f1, use_container_width=True)
            with tc2:
                f2 = go.Figure()
                f2.add_trace(go.Scatter(x=tdf['date'], y=tdf['healthy'], mode='lines',
                                        name='健康', stackgroup='one', line=dict(color='#06A77D')))
                f2.add_trace(go.Scatter(x=tdf['date'], y=tdf['low_ctr'], mode='lines',
                                        name='低CTR', stackgroup='one', line=dict(color='#F18F01')))
                f2.add_trace(go.Scatter(x=tdf['date'], y=tdf['low_cvr'], mode='lines',
                                        name='低CVR', stackgroup='one', line=dict(color='#A23B72')))
                f2.add_trace(go.Scatter(x=tdf['date'], y=tdf['no_imp'], mode='lines',
                                        name='零曝光', stackgroup='one', line=dict(color='#7B7B7B')))
                f2.update_layout(title='14 天漏斗段分布', height=300,
                                 margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(f2, use_container_width=True)
        else:
            st.caption("📈 趋势曲线: 至少需要 2 天 cro_snapshots 数据 (daily_tasks 09:30 自动落表)")
    except Exception as _e:
        st.caption(f"趋势曲线加载失败: {_e}")

    # ─── 漏斗分布饼图 ───
    if by_stage:
        try:
            import plotly.express as px
            import pandas as pd
            stage_label = {
                'healthy': '健康',
                'low_ctr': '曝光→点击漏',
                'low_cvr': '点击→下单漏',
                'no_impression': '零曝光',
                'insufficient_data': '数据不足',
            }
            stage_df = pd.DataFrame([
                {'stage': stage_label.get(k, k), 'count': v}
                for k, v in by_stage.items()
            ])
            fig = px.pie(stage_df, names='stage', values='count',
                         title='漏斗阶段分布', hole=0.4,
                         color_discrete_sequence=px.colors.qualitative.Set2)
            fig.update_layout(height=320)
            st.plotly_chart(fig, use_container_width=True)
        except ImportError:
            pass

    # ─── 优先动作清单 ───
    st.markdown("### 🚀 优先动作清单")
    st.caption("按优先级 + 曝光量排序；表格是预览，执行按钮会处理完整 P1 安全动作集合。")

    from collections import Counter
    from src.services.cro_auto_executor import (
        SAFE_AUTO_ACTIONS, auto_enqueue_and_execute,
        write_auto_execution_report,
    )
    from src.services.cro_action_queue import (
        enqueue_unique_pending, load_pending, queue_stats, recent_terminal_keys,
    )

    all_action_rows = top_actions(diagnoses, limit=10000)
    action_counts = Counter(r.get('action') for r in all_action_rows)
    p1_action_counts = Counter(
        r.get('action') for r in all_action_rows if r.get('priority') == 1
    )
    recently_handled_keys = recent_terminal_keys(hours=24)
    executable_safe_p1_count = sum(
        1 for row in all_action_rows
        if row.get('priority') == 1
        and row.get('action') in SAFE_AUTO_ACTIONS
        and (str(row.get('sku') or '').strip(), str(row.get('action') or '').strip())
        not in recently_handled_keys
    )

    if action_counts:
        try:
            import pandas as pd
            action_summary = pd.DataFrame([
                {
                    '动作': action,
                    '诊断建议': action_counts.get(action, 0),
                    'P1': p1_action_counts.get(action, 0),
                    '24h已处理': sum(
                        1 for row in all_action_rows
                        if row.get('action') == action
                        and row.get('priority') == 1
                        and (str(row.get('sku') or '').strip(), str(row.get('action') or '').strip())
                        in recently_handled_keys
                    ),
                    '自动执行范围': '仅 P1' if action in SAFE_AUTO_ACTIONS else '否',
                }
                for action in sorted(action_counts)
            ])
            st.dataframe(action_summary, use_container_width=True,
                         hide_index=True, height=190)
        except Exception:
            st.caption(f"动作分布: {dict(action_counts)}")

    action_filter = st.selectbox(
        "筛选动作类型",
        options=['全部', 'price_drop', 'title_refresh', 'image_refresh',
                 'fill_specifics', 'promote', 'delist'],
        format_func=lambda x: {
            '全部': '全部', 'price_drop': '💰 改价',
            'title_refresh': '✏️ 改标题', 'image_refresh': '🖼️ 换主图',
            'fill_specifics': '📋 补 specifics', 'promote': '📢 推广',
            'delist': '🗑️ 下架',
        }.get(x, x),
        key='cro_action_filter',
    )
    af = None if action_filter == '全部' else action_filter
    filtered_rows = [
        r for r in all_action_rows
        if af is None or r.get('action') == af
    ]
    def _action_key(row):
        return (
            str(row.get('sku') or '').strip(),
            str(row.get('action') or '').strip(),
        )

    pending_safe_rows = []
    pending_executable_safe_rows = []
    for safe_action in SAFE_AUTO_ACTIONS:
        pending_safe_rows.extend(load_pending(action_type=safe_action, include_control=True))
        pending_executable_safe_rows.extend(load_pending(action_type=safe_action))
    pending_safe_keys = {_action_key(row) for row in pending_safe_rows}
    pending_executable_safe_keys = {_action_key(row) for row in pending_executable_safe_rows}
    pending_control_safe_keys = pending_safe_keys - pending_executable_safe_keys
    filtered_safe_p1_keys = [
        _action_key(row) for row in filtered_rows
        if row.get('priority') == 1 and row.get('action') in SAFE_AUTO_ACTIONS
    ]
    filtered_pending_executable_safe_p1_count = sum(
        1 for key in filtered_safe_p1_keys if key in pending_executable_safe_keys
    )
    filtered_pending_control_safe_p1_count = sum(
        1 for key in filtered_safe_p1_keys if key in pending_control_safe_keys
    )
    filtered_recent_terminal_safe_p1_count = sum(
        1 for key in filtered_safe_p1_keys if key in recently_handled_keys
    )
    filtered_not_queued_safe_p1_count = sum(
        1 for key in filtered_safe_p1_keys
        if key not in pending_safe_keys and key not in recently_handled_keys
    )
    filtered_actionable_safe_p1_count = (
        filtered_pending_executable_safe_p1_count + filtered_not_queued_safe_p1_count
    )
    preview_limit = 200
    rows = filtered_rows[:preview_limit]
    delist_confirmation_rows = []
    if action_filter == 'delist':
        delist_confirmation_rows = _render_cro_delist_confirmation_panel(limit=500)

    if not rows:
        if action_filter == 'delist' and delist_confirmation_rows:
            pass
        else:
            st.success("🎉 当前没有待处理的转化率优化动作 (或数据不足)")
    else:
        total_filtered = len(filtered_rows)
        p1_rows = [r for r in filtered_rows if r.get('priority') == 1]
        p1_safe_rows = [r for r in p1_rows if r.get('action') in SAFE_AUTO_ACTIONS]
        manual_only_count = len(p1_rows) - len(p1_safe_rows)
        st.caption(
            f"当前筛选共有 {total_filtered} 条建议；表格显示前 {len(rows)} 条。"
            f"P1 共 {len(p1_rows)} 条，其中安全动作 {len(p1_safe_rows)} 条，"
            f"已在队列可执行 {filtered_pending_executable_safe_p1_count} 条，"
            f"对照组 {filtered_pending_control_safe_p1_count} 条，"
            f"尚未入队 {filtered_not_queued_safe_p1_count} 条，"
            f"24h 已处理 {filtered_recent_terminal_safe_p1_count} 条。"
        )
        try:
            import pandas as pd
            df_act = pd.DataFrame(rows)
            df_act['CTR'] = (df_act['ctr'] * 100).round(2).astype(str) + '%'
            df_act['CVR'] = (df_act['cvr'] * 100).round(2).astype(str) + '%'
            df_act['优先级'] = df_act['priority'].map({1: '🔴 P1', 2: '🟠 P2', 3: '🟡 P3', 4: '🔵 P4', 5: '⚪ P5'})
            def _execution_mode(row):
                action = row.get('action')
                priority = int(row.get('priority') or 0)
                if action == 'delist':
                    return '人工确认'
                if priority == 1 and action in SAFE_AUTO_ACTIONS:
                    return 'P1 自动'
                if priority == 1:
                    return 'P1 人工'
                return '非 P1 不自动'
            df_act['执行方式'] = df_act.apply(_execution_mode, axis=1)
            df_show = df_act[['优先级', 'sku', 'action', 'reason', 'expected_lift',
                              'cro_score', 'impressions', 'CTR', 'CVR', '执行方式']].copy()
            df_show.columns = ['优先级', 'SKU', '动作', '原因', '预期提升',
                               'CRO 分', '展示', 'CTR', 'CVR', '执行方式']
            st.dataframe(df_show, use_container_width=True, hide_index=True, height=420)
        except Exception:
            st.dataframe(rows, use_container_width=True)

        # ─── 一键入队 / 执行 ───
        col_q1, col_q2, col_q3 = st.columns([1, 1.25, 1.75])
        with col_q1:
            if st.button(f"🚀 P1 入队 ({len(p1_rows)})",
                         disabled=not p1_rows, key='cro_enqueue_p1'):
                res = enqueue_unique_pending(p1_rows, source='cro_ui')
                st.success(
                    f"新增入队 {res['added']} 条；已在队列中跳过 {res['skipped_duplicate']} 条。"
                )
        with col_q2:
            qs = queue_stats()
            st.caption(
                f"队列: 总 {qs['total']} · 可执行待处理 {qs.get('pending_executable', qs['pending'])} · "
                f"对照组 {qs.get('pending_control', 0)} · 已完成 {qs['done']} · 跳过 {qs.get('skipped', 0)}"
            )
            pending_by_action = qs.get('pending_by_action') or {}
            pending_executable_by_action = qs.get('pending_executable_by_action') or {}
            if pending_executable_by_action:
                st.caption("可执行待处理分布: " + " · ".join(
                    f"{k} {v}" for k, v in sorted(pending_executable_by_action.items())
                ))
            elif qs.get('pending_control', 0) and pending_by_action:
                st.caption("当前没有可执行待处理；对照组待观察分布: " + " · ".join(
                    f"{k} {v}" for k, v in sorted(pending_by_action.items())
                ))
        with col_q3:
            send_auto_email = st.checkbox(
                "执行后发邮件",
                value=True,
                key='cro_auto_execute_email',
            )
            if st.button(
                f"⚡ 入队并执行安全 P1 (可执行 {filtered_pending_executable_safe_p1_count} / 待入队 {filtered_not_queued_safe_p1_count})",
                disabled=not filtered_actionable_safe_p1_count,
                key='cro_auto_execute_p1',
            ):
                with st.spinner("正在执行 CRO 安全动作..."):
                    rep = auto_enqueue_and_execute(
                        filtered_rows,
                        action_types=SAFE_AUTO_ACTIONS,
                        enqueue_limit=200,
                        apply_changes=True,
                        send_email=send_auto_email,
                    )
                    report_path = write_auto_execution_report(rep)
                action_summaries = rep.get('execution', {}).get('actions', [])
                total_done = sum(a.get('done', 0) for a in action_summaries)
                total_failed = sum(a.get('failed', 0) for a in action_summaries)
                total_skipped = sum(a.get('skipped', 0) for a in action_summaries)
                if total_failed:
                    st.warning(
                        f"自动执行完成: 成功 {total_done} · 失败 {total_failed} · 跳过 {total_skipped}。"
                    )
                else:
                    st.success(
                        f"自动执行完成: 成功 {total_done} · 跳过 {total_skipped}。"
                    )
                st.caption(f"报告已写入: {report_path}")
                try:
                    import pandas as pd
                    st.dataframe(
                        pd.DataFrame([
                            {
                                '动作': a.get('action'),
                                '待处理': a.get('pending_total', 0),
                                '成功': a.get('done', 0),
                                '失败': a.get('failed', 0),
                                '跳过': a.get('skipped', 0),
                                '标记处理': a.get('marked_done', 0) + a.get('marked_skipped', 0),
                            }
                            for a in action_summaries
                        ]),
                        use_container_width=True,
                        hide_index=True,
                    )
                except Exception:
                    st.json(action_summaries)
            if manual_only_count:
                st.caption(f"P1 中另有 {manual_only_count} 条需人工确认或暂无执行器。")

    # ─── 单 SKU 深入诊断 ───
    st.markdown("---")
    st.markdown("### 🔍 单 SKU 漏斗深入")
    sku_options = [d.sku for d in diagnoses if d.impressions > 0]
    if sku_options:
        sel = st.selectbox("选 SKU", sku_options, key='cro_sku_select')
        d = next((x for x in diagnoses if x.sku == sel), None)
        if d:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("展示", f"{d.impressions:,}")
            c2.metric("浏览", f"{d.views:,}", delta=f"CTR {d.ctr*100:.2f}%")
            c3.metric("成交", d.transactions or d.sold_qty,
                      delta=f"CVR {d.cvr*100:.2f}%")
            c4.metric("CRO 分", f"{d.cro_score}/100")
            st.info(f"**漏斗阶段**: {d.funnel_stage} — {d.bottleneck}")
            if d.actions:
                st.markdown("**建议动作**:")
                for a in d.actions:
                    st.markdown(f"- **P{a.priority} · {a.type}** — {a.reason}  \n"
                                f"  → 预期: {a.expected_lift}")
                    if a.detail:
                        st.json(a.detail)
            else:
                st.success("此 SKU 漏斗健康, 暂无优化动作")
    else:
        st.info("还没有 SKU 进入诊断 (需要展示 > 0)")
