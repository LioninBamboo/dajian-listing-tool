"""
eBay 广告监控服务

功能：
1. 获取所有活跃广告 campaign 及其 listing
2. 合并流量/销售数据计算广告 ROAS
3. 生成广告开关建议（开启/关闭/调整竞价率）
"""
import os
import sys
import json
import time
import logging
import requests
import warnings
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")
from src.utils.runtime_cache import get_runtime_cache_path, resolve_runtime_cache_path

warnings.filterwarnings('ignore')
logger = logging.getLogger(__name__)

# 广告数据缓存
AD_CACHE_FILE = get_runtime_cache_path('ad_data_cache.json')
LEGACY_AD_CACHE_FILE = PROJECT_ROOT / '_ad_data_cache.json'
AD_CACHE_TTL_HOURS = 4


def _calc_margin_on_actual_price(actual_price, total_cost, ad_rate,
                                 ebay_fee_rate=0.1325, fixed_fee=0.30):
    """Margin denominator = actual transaction price, with ad cost based on actual bid%."""
    if actual_price <= 0 or total_cost <= 0:
        return 0.0
    net_profit = actual_price - actual_price * ebay_fee_rate - actual_price * ad_rate - fixed_fee - total_cost
    return net_profit / actual_price


class EbayAdService:
    """eBay 广告/推广数据服务"""

    def __init__(self):
        from src.services.ebay_auth import EbayOAuthService
        self.oauth = EbayOAuthService(os.getenv('EBAY_ENVIRONMENT', 'PRODUCTION'))
        self.base = self.oauth.api_base

    def _headers(self):
        token = self.oauth.get_valid_token()
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US',
        }

    def _api_get(self, url, params=None, retries=3):
        for i in range(retries):
            try:
                r = requests.get(url, headers=self._headers(), params=params,
                                 timeout=60, verify=False)
                return r
            except Exception as e:
                if i < retries - 1:
                    time.sleep(3)
                    continue
                logger.error(f"API GET failed: {e}")
                return None

    # ─── 获取广告 campaign 列表 ───

    def fetch_campaigns(self, status='RUNNING'):
        """获取广告活动列表"""
        campaigns = []
        offset = 0
        while True:
            r = self._api_get(
                f"{self.base}/sell/marketing/v1/ad_campaign",
                params={'campaign_status': status, 'limit': '100', 'offset': str(offset)}
            )
            if not r or r.status_code != 200:
                break
            data = r.json()
            batch = data.get('campaigns', [])
            campaigns.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        return campaigns

    # ─── 获取 campaign 内的广告链接 ───

    def fetch_campaign_ads(self, campaign_id):
        """获取单个 campaign 内所有 ad (promoted listing)"""
        ads = []
        offset = 0
        while True:
            r = self._api_get(
                f"{self.base}/sell/marketing/v1/ad_campaign/{campaign_id}/ad",
                params={'limit': '500', 'offset': str(offset)}
            )
            if not r or r.status_code != 200:
                break
            data = r.json()
            batch = data.get('ads', [])
            ads.extend(batch)
            total = data.get('total', 0)
            if offset + len(batch) >= total:
                break
            offset += len(batch)
        return ads

    # ─── 创建/删除广告 ───

    def create_ad(self, campaign_id, listing_id, bid_percentage=5.0):
        """向 campaign 添加一个 ad (promoted listing)"""
        url = f"{self.base}/sell/marketing/v1/ad_campaign/{campaign_id}/ad"
        body = {
            "listingId": str(listing_id),
            "bidPercentage": str(bid_percentage),
        }
        try:
            r = requests.post(url, headers=self._headers(), json=body, timeout=30, verify=False)
            if r.status_code in (200, 201):
                logger.info(f"✅ 已开启广告: listing={listing_id} → campaign={campaign_id}")
                return {'success': True, 'listing_id': listing_id}
            else:
                err = r.text[:300]
                logger.error(f"❌ 创建广告失败 {r.status_code}: {err}")
                return {'success': False, 'error': f"HTTP {r.status_code}: {err}"}
        except Exception as e:
            logger.error(f"❌ 创建广告异常: {e}")
            return {'success': False, 'error': str(e)}

    def create_ad_safe(self, campaign_id, listing_id, sku=None,
                       bid_percentage=5.0, safety_margin=0.05,
                       db_path='ebay_collection.db'):
        """安全创建广告: 先用 PricingEngine.required_ad_rate_for 校验现价能否撑得起.

        若 SKU 当前 listing price 在扣完 fees + bid_percentage% 广告费后
        仍能保留 safety_margin 利润 → 调 create_ad
        否则 → 拒绝创建, 返回 {'success': False, 'reason': 'unsafe', ...}

        被 batch_publish / 手动开广告 / ad_restore_audit 共用, 杜绝
        "新刊登立刻开 5% 广告 → 守门员立刻关掉" 的反复.

        Args:
            campaign_id: 目标 campaign
            listing_id: eBay item id
            sku: 商品 SKU (用于查 cost; 若 None 则按 sku=listing_id 在 DB 找不到)
            bid_percentage: 期望 bid (默认 5.0)
            safety_margin: 净利润缓冲 (默认 0.05 即 5%)
            db_path: ebay_collection.db 路径

        Returns:
            dict: {'success': bool, 'listing_id': str, ...}
                  当 success=False 时含 'reason' (unsafe / no_cost / no_price / api_failed)
        """
        from src.services.repricing_guard import _fetch_cost_and_listing
        from src.services.pricing_engine import PricingEngine
        from src.services import ad_blacklist

        # 找 SKU + cost
        if not sku:
            # 用 listing_id 反查 SKU
            try:
                import sqlite3
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    "SELECT sku FROM collected_products WHERE listing_id = ? LIMIT 1",
                    (str(listing_id),),
                )
                row = cur.fetchone()
                conn.close()
                if row:
                    sku = row[0]
            except Exception:
                pass

        # P4: 黑名单优先级最高 — 即使数据齐全也不开
        if sku and ad_blacklist.is_blacklisted(sku):
            entry = ad_blacklist.get_entry(sku) or {}
            msg = (f"SKU 在广告黑名单 (reason={entry.get('reason')}, "
                   f"off_count={entry.get('off_count', 0)})")
            logger.warning(f"📛 create_ad_safe {sku} (listing={listing_id}): 拒绝 - {msg}")
            return {
                'success': False, 'listing_id': listing_id, 'sku': sku,
                'reason': 'blacklisted', 'error': msg,
                'blacklist_entry': entry,
            }

        if not sku:
            logger.warning(f"⚠️ create_ad_safe: 找不到 listing={listing_id} 对应 SKU, fail-open 直接调 create_ad")
            return self.create_ad(campaign_id, listing_id, bid_percentage)

        cost, _ = _fetch_cost_and_listing(db_path, sku)
        if not cost or cost <= 0:
            logger.warning(f"⚠️ create_ad_safe {sku}: 无 cost 数据, fail-open 直接调 create_ad")
            return self.create_ad(campaign_id, listing_id, bid_percentage)

        # 取现网现价
        try:
            from src.services.ebay_auth import EbayOAuthService
            oauth = EbayOAuthService('PRODUCTION')
            token = oauth.get_valid_token()
            r = requests.get(
                f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}",
                headers={'Authorization': f'Bearer {token}'},
                timeout=30, verify=False,
            )
            live_price = None
            if r.status_code == 200:
                for o in r.json().get('offers', []):
                    p = o.get('pricingSummary', {}).get('price', {}).get('value')
                    if p:
                        live_price = float(p)
                        break
        except Exception as e:
            logger.warning(f"⚠️ create_ad_safe {sku}: 取现网价失败 ({e}), fail-open")
            return self.create_ad(campaign_id, listing_id, bid_percentage)

        if not live_price:
            logger.warning(f"⚠️ create_ad_safe {sku}: 无 live_price, fail-open")
            return self.create_ad(campaign_id, listing_id, bid_percentage)

        max_ad = PricingEngine.required_ad_rate_for(live_price, cost, safety_margin)
        target = bid_percentage / 100.0
        if max_ad + 0.0001 < target:
            msg = (f"现价 ${live_price:.2f} 撑不起 {bid_percentage:.1f}% 广告 "
                   f"(最大可承受 {max_ad*100:.2f}%, cost=${cost:.2f}, "
                   f"safety={safety_margin*100:.0f}%)")
            logger.warning(f"🚫 create_ad_safe {sku} (listing={listing_id}): 拒绝开广告 - {msg}")
            return {
                'success': False, 'listing_id': listing_id, 'sku': sku,
                'reason': 'unsafe', 'error': msg,
                'live_price': live_price, 'total_cost': cost,
                'required_ad_rate': max_ad, 'target_ad_rate': target,
            }

        return self.create_ad(campaign_id, listing_id, bid_percentage)

    def delete_ad(self, campaign_id, listing_id):
        """从 campaign 删除一个 ad"""
        # 需要先获取 ad_id (使用缓存)
        ad_lookup = self._build_ad_lookup(campaign_id)
        ad_id = ad_lookup.get(str(listing_id), '')
        if not ad_id:
            return {'success': False, 'error': f'Ad not found for listing {listing_id} in campaign {campaign_id}'}

        url = f"{self.base}/sell/marketing/v1/ad_campaign/{campaign_id}/ad/{ad_id}"
        try:
            r = requests.delete(url, headers=self._headers(), timeout=30, verify=False)
            if r.status_code in (200, 204):
                logger.info(f"✅ 已关闭广告: listing={listing_id}, ad={ad_id}")
                return {'success': True, 'listing_id': listing_id}
            else:
                err = r.text[:300]
                logger.error(f"❌ 删除广告失败 {r.status_code}: {err}")
                return {'success': False, 'error': f"HTTP {r.status_code}: {err}"}
        except Exception as e:
            logger.error(f"❌ 删除广告异常: {e}")
            return {'success': False, 'error': str(e)}

    def batch_create_ads(self, campaign_id, listing_ids, bid_percentage=5.0):
        """批量创建广告"""
        results = []
        for lid in listing_ids:
            r = self.create_ad(campaign_id, lid, bid_percentage)
            results.append(r)
            time.sleep(0.5)
        return results

    def batch_delete_ads(self, campaign_id, listing_ids):
        """批量删除广告"""
        results = []
        for lid in listing_ids:
            r = self.delete_ad(campaign_id, lid)
            results.append(r)
            time.sleep(0.5)
        return results

    # ─── 更新广告竞价 ───

    def _build_ad_lookup(self, campaign_id, _cache={}):
        """
        获取 campaign 的 listing_id → ad_id 映射 (带内存缓存, 同一次批量操作不重复获取)
        """
        cache_key = campaign_id
        cached = _cache.get(cache_key)
        if cached and (time.time() - cached['ts']) < 120:  # 2 分钟内有效
            return cached['map']

        ads = self.fetch_campaign_ads(campaign_id)
        mapping = {}
        for ad in ads:
            lid = ad.get('listingId', '')
            if lid:
                mapping[lid] = ad.get('adId', '')

        _cache[cache_key] = {'map': mapping, 'ts': time.time()}
        return mapping

    # ─── 跨 campaign 查找单个 listing 的广告 (用于重定价守门员) ───

    def find_ad_for_listing(self, listing_id, _xc={}):
        """
        在所有 RUNNING campaign 中查找指定 listing 是否有活跃广告.

        被 update_ebay_price 守门员调用: 当价格触底需要决定是否关广告时,
        先用此方法查清楚 listing 是否真的在被推广.

        Args:
            listing_id: eBay item id
            _xc: 跨 campaign 缓存 (单进程共享, 5 分钟 TTL)

        Returns:
            {'campaign_id': str, 'ad_id': str, 'bid_percentage': float} 或 None
        """
        listing_id = str(listing_id or '')
        if not listing_id:
            return None

        cached = _xc.get('all_ads')
        if cached and (time.time() - cached['ts']) < 300:  # 5 分钟
            return cached['map'].get(listing_id)

        # 重建全量缓存
        try:
            campaigns = self.fetch_campaigns(status='RUNNING')
        except Exception as e:
            logger.warning(f"find_ad_for_listing: fetch_campaigns failed: {e}")
            return None

        all_ads_map = {}
        for c in campaigns:
            cid = c.get('campaignId', '')
            if not cid:
                continue
            try:
                ads = self.fetch_campaign_ads(cid)
            except Exception:
                continue
            for ad in ads:
                lid = str(ad.get('listingId', '') or '')
                if not lid:
                    continue
                # 同一 listing 可能在多个 campaign — 取第一个 (保留竞价信息)
                if lid not in all_ads_map:
                    bid = 0.0
                    try:
                        bid = float(ad.get('bidPercentage', 0))
                    except (TypeError, ValueError):
                        pass
                    all_ads_map[lid] = {
                        'campaign_id': cid,
                        'ad_id': ad.get('adId', ''),
                        'bid_percentage': bid,
                    }
        _xc['all_ads'] = {'map': all_ads_map, 'ts': time.time()}
        return all_ads_map.get(listing_id)

    def is_listing_promoted(self, listing_id) -> bool:
        """轻量包装: 判断 listing 当前是否在被推广."""
        return self.find_ad_for_listing(listing_id) is not None

    def update_ad_bid(self, campaign_id, listing_id, new_bid_percentage, _ad_lookup=None):
        """
        更新单个广告的竞价率

        eBay API: POST /sell/marketing/v1/ad_campaign/{campaign_id}/ad/{ad_id}/update_bid
        需要先通过 listing_id 查找 ad_id。

        Args:
            _ad_lookup: 预构建的 {listing_id: ad_id} 映射, 避免重复 fetch
        """
        # 查找 ad_id (使用缓存)
        if _ad_lookup is not None:
            ad_id = _ad_lookup.get(str(listing_id), '')
        else:
            ad_lookup = self._build_ad_lookup(campaign_id)
            ad_id = ad_lookup.get(str(listing_id), '')

        if not ad_id:
            return {'success': False,
                    'error': f'Ad not found for listing {listing_id} in campaign {campaign_id}',
                    'listing_id': listing_id}

        url = f"{self.base}/sell/marketing/v1/ad_campaign/{campaign_id}/ad/{ad_id}/update_bid"
        body = {"bidPercentage": str(new_bid_percentage)}
        try:
            r = requests.post(url, headers=self._headers(), json=body, timeout=30, verify=False)
            if r.status_code in (200, 204):
                logger.info(f"✅ 已更新竞价: listing={listing_id}, bid={new_bid_percentage}%")
                return {'success': True, 'listing_id': listing_id, 'new_bid': new_bid_percentage}
            else:
                err = r.text[:500]
                logger.error(f"❌ 更新竞价失败 {r.status_code}: {err}")
                return {'success': False, 'error': f"HTTP {r.status_code}: {err}", 'listing_id': listing_id}
        except Exception as e:
            logger.error(f"❌ 更新竞价异常: {e}")
            return {'success': False, 'error': str(e), 'listing_id': listing_id}

    def bulk_update_ads_bid_by_listing_id(self, campaign_id, listing_bid_pairs):
        """
        使用 eBay bulk API 批量更新竞价 (一次 API 调用更新多个)

        eBay API: POST /sell/marketing/v1/ad_campaign/{campaign_id}/bulk_update_ads_bid_by_listing_id

        Args:
            campaign_id: str
            listing_bid_pairs: list of {'listing_id': str, 'bid_percentage': float}
        Returns:
            dict with 'success_count', 'fail_count', 'errors' list
        """
        url = f"{self.base}/sell/marketing/v1/ad_campaign/{campaign_id}/bulk_update_ads_bid_by_listing_id"
        requests_body = [
            {
                "listingId": str(pair['listing_id']),
                "bidPercentage": str(pair['bid_percentage']),
            }
            for pair in listing_bid_pairs
        ]
        body = {"requests": requests_body}

        try:
            r = requests.post(url, headers=self._headers(), json=body, timeout=120, verify=False)
            if r.status_code in (200, 207):
                data = r.json()
                responses = data.get('responses', [])
                ok = sum(1 for resp in responses if resp.get('statusCode') in (200, 204))
                fail = len(responses) - ok
                errors = [
                    {'listing_id': resp.get('listingId', '?'),
                     'error': resp.get('errors', [{}])[0].get('message', 'Unknown') if resp.get('errors') else ''}
                    for resp in responses if resp.get('statusCode') not in (200, 204)
                ]
                logger.info(f"✅ 批量更新完成: {ok} 成功, {fail} 失败")
                return {'success_count': ok, 'fail_count': fail, 'errors': errors}
            else:
                err = r.text[:500]
                logger.error(f"❌ 批量更新失败 {r.status_code}: {err}")
                return {'success_count': 0, 'fail_count': len(listing_bid_pairs),
                        'errors': [{'error': f'HTTP {r.status_code}: {err}'}]}
        except Exception as e:
            logger.error(f"❌ 批量更新异常: {e}")
            return {'success_count': 0, 'fail_count': len(listing_bid_pairs),
                    'errors': [{'error': str(e)}]}

    def batch_update_ad_bids(self, items, new_bid_percentage):
        """
        批量更新竞价率 (智能选择: 优先用 bulk API, 失败后逐个更新)

        Args:
            items: list of dicts with 'campaign_id' and 'listing_id'
            new_bid_percentage: float, e.g. 7.0
        Returns:
            list of result dicts
        """
        # 按 campaign_id 分组
        by_campaign = {}
        results = []
        for item in items:
            cid = item.get('campaign_id', '')
            lid = item.get('listing_id', '')
            if not cid or not lid:
                results.append({'success': False, 'listing_id': lid, 'error': 'Missing campaign_id or listing_id'})
                continue
            by_campaign.setdefault(cid, []).append(lid)

        for cid, lids in by_campaign.items():
            # 先尝试 bulk API
            pairs = [{'listing_id': lid, 'bid_percentage': new_bid_percentage} for lid in lids]
            bulk_result = self.bulk_update_ads_bid_by_listing_id(cid, pairs)

            if bulk_result['success_count'] > 0 or bulk_result['fail_count'] == 0:
                # Bulk API worked (at least partially)
                for lid in lids:
                    # Check individual results if available
                    failed_lids = {e.get('listing_id') for e in bulk_result.get('errors', [])}
                    if lid in failed_lids:
                        err_msg = next(
                            (e['error'] for e in bulk_result['errors'] if e.get('listing_id') == lid),
                            'Bulk update failed'
                        )
                        results.append({'success': False, 'listing_id': lid, 'error': err_msg})
                    else:
                        results.append({'success': True, 'listing_id': lid, 'new_bid': new_bid_percentage})
            else:
                # Bulk API not supported or completely failed → 逐个更新 (带缓存)
                logger.warning(f"Bulk API 不可用, 逐个更新 campaign {cid} 的 {len(lids)} 个广告")
                ad_lookup = self._build_ad_lookup(cid)
                for lid in lids:
                    r = self.update_ad_bid(cid, lid, new_bid_percentage, _ad_lookup=ad_lookup)
                    results.append(r)
                    time.sleep(0.3)

        return results

    # ─── 汇总所有广告数据 ───

    def fetch_all_ad_data(self):
        """
        获取所有活跃 campaign 的广告数据

        Returns:
            dict: {
                'campaigns': [{id, name, status, bid_percentage, ad_count}],
                'ads': {listing_id: {bid_percentage, campaign_name, ...}},
                'fetched_at': ISO timestamp,
            }
        """
        campaigns_raw = self.fetch_campaigns('RUNNING')
        logger.info(f"活跃 campaign: {len(campaigns_raw)}")

        campaigns = []
        ads_by_listing = {}

        for camp in campaigns_raw:
            cid = camp['campaignId']
            cname = camp.get('campaignName', '?')
            bid_pct = camp.get('fundingStrategy', {}).get('bidPercentage', '5.0')

            raw_ads = self.fetch_campaign_ads(cid)

            for ad in raw_ads:
                lid = ad.get('listingId', '')
                if not lid:
                    continue
                ads_by_listing[lid] = {
                    'listing_id': lid,
                    'bid_percentage': float(ad.get('bidPercentage', bid_pct) or bid_pct),
                    'campaign_id': cid,
                    'campaign_name': cname,
                    'ad_status': ad.get('status', ''),
                }

            campaigns.append({
                'id': cid,
                'name': cname,
                'status': camp.get('campaignStatus', ''),
                'bid_percentage': float(bid_pct) if bid_pct and bid_pct != '?' else 5.0,
                'ad_count': len(raw_ads),
                'funding_model': camp.get('fundingStrategy', {}).get('fundingModel', ''),
                'start_date': camp.get('startDate', ''),
            })

            time.sleep(0.3)

        result = {
            'campaigns': campaigns,
            'ads': ads_by_listing,
            'fetched_at': datetime.now().isoformat(),
        }
        logger.info(f"总推广链接: {len(ads_by_listing)}")
        return result


def save_ad_cache(data):
    """保存广告数据缓存"""
    with open(AD_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'campaigns': data['campaigns'],
            'ads': {k: v for k, v in data['ads'].items()},
            'fetched_at': data['fetched_at'],
        }, f, ensure_ascii=False, indent=2)


def load_ad_cache(force_refresh=False):
    """加载广告数据缓存，过期则刷新"""
    cache_path = resolve_runtime_cache_path(
        'ad_data_cache.json',
        legacy_filename=LEGACY_AD_CACHE_FILE.name,
    )

    if not force_refresh and cache_path.exists():
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            fetched = datetime.fromisoformat(cache.get('fetched_at', '2020-01-01'))
            if (datetime.now() - fetched).total_seconds() < AD_CACHE_TTL_HOURS * 3600:
                return cache
        except Exception:
            pass

    # Refresh
    try:
        svc = EbayAdService()
        data = svc.fetch_all_ad_data()
        save_ad_cache(data)
        return data
    except Exception as e:
        logger.error(f"获取广告数据失败: {e}")
        # Return stale cache if available
        if cache_path.exists():
            with open(cache_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None


# ═══════════════════ 广告建议引擎 ═══════════════════

def generate_ad_recommendations(products, ad_data, perf_data=None):
    """
    根据表现数据生成每个链接的广告建议

    建议类型:
    - KEEP_AD: 正在推广且表现好 → 继续
    - STOP_AD: 正在推广但表现差 → 建议关闭
    - START_AD: 未推广但有潜力 → 建议开广告
    - INCREASE_BID: 推广中但曝光不足 → 提高竞价
    - NO_CHANGE: 无需调整

    判断逻辑 (基于5%广告预算):
                     有销售  高展示(>200)  高CTR(>1%)
    KEEP_AD:          Yes     Yes           -
    STOP_AD:          No      Yes           No (<0.5%)
    START_AD:         No      No            -          (利润率>10%, 未推广)
    INCREASE_BID:     No      No            -          (已推广, 展示<100)
    """
    raw_ads = ad_data.get('ads', {}) if ad_data else {}
    # Handle both dict (keyed by listing_id) and list formats
    if isinstance(raw_ads, list):
        ads = {a['listing_id']: a for a in raw_ads if 'listing_id' in a}
    else:
        ads = raw_ads
    recommendations = []

    for p in products:
        lid = p.get('listing_id', '')
        sku = p.get('sku', '')
        impressions = p.get('impressions', 0)
        views = p.get('views', 0)
        ctr = p.get('ctr', 0)
        sold_qty = p.get('sold_qty', 0)
        revenue = p.get('revenue', 0)
        selling_price = p.get('selling_price', 0)
        total_cost = p.get('total_cost', 0)
        has_ad = lid in ads

        ad_info = ads.get(lid, {})
        bid_pct = ad_info.get('bid_percentage', 5.0) / 100.0 if has_ad else 0.05
        campaign_name = ad_info.get('campaign_name', '') if has_ad else ''
        net_margin = _calc_margin_on_actual_price(selling_price, total_cost, bid_pct)

        # 估算广告费用: 只有成交时才扣费 (PLS = Cost Per Sale)
        est_ad_cost = selling_price * bid_pct * sold_qty if sold_qty > 0 else 0
        roas = revenue / est_ad_cost if est_ad_cost > 0 else 0

        # 生成建议
        action = 'NO_CHANGE'
        reason = ''
        priority = 0  # 0=low, 1=medium, 2=high

        # 关键阈值: 利润率必须 > 广告竞价率 + 2% 才值得推广
        min_margin_for_ad = bid_pct + 0.02  # 5%bid → 需要至少7%利润

        if has_ad:
            if net_margin < bid_pct:
                # ⚠️ 利润率连广告费都覆盖不了 → 必须关闭
                action = 'STOP_AD'
                reason = f'利润率{net_margin:.1%} < 竞价率{bid_pct:.0%}, 每成交一单都亏广告费, 必须关闭'
                priority = 2
            elif net_margin < min_margin_for_ad and sold_qty == 0:
                # 利润率勉强覆盖广告，但没销售 → 关闭
                action = 'STOP_AD'
                reason = f'利润率{net_margin:.1%}仅勉强覆盖{bid_pct:.0%}广告费且无销售, 建议关闭'
                priority = 2
            elif sold_qty > 0 and net_margin >= bid_pct:
                # 有广告+有销售+利润够 → 继续推广
                action = 'KEEP_AD'
                reason = f'广告有效: 售出{sold_qty}件, 销售额${revenue:.0f}, ROAS={roas:.1f}x'
                priority = 0
            elif sold_qty > 0 and net_margin < bid_pct:
                # 有销售但利润不够覆盖广告
                action = 'STOP_AD'
                reason = f'虽有{sold_qty}件成交, 但利润{net_margin:.1%}<广告费{bid_pct:.0%}, 关闭后仍能自然成交'
                priority = 2
            elif impressions >= 500 and ctr < 0.005:
                # 高展示低CTR → 建议关闭
                action = 'STOP_AD'
                reason = f'高展示({impressions})但CTR极低({ctr:.2%}), 建议关闭广告节省费用'
                priority = 2
            elif impressions >= 200 and views == 0:
                # 有展示但零浏览
                action = 'STOP_AD'
                reason = f'展示{impressions}次但零浏览, 标题/图片需优化后再推广'
                priority = 2
            elif impressions < 100 and net_margin >= min_margin_for_ad:
                # 低展示但利润够 → 可考虑提高竞价
                action = 'INCREASE_BID'
                reason = f'展示仅{impressions}次, 利润{net_margin:.1%}充足, 考虑提高bid到7-8%'
                priority = 1
            elif impressions < 100 and net_margin < min_margin_for_ad:
                # 低展示且利润不够提高竞价
                action = 'STOP_AD'
                reason = f'展示仅{impressions}, 利润{net_margin:.1%}不足以支撑更高竞价, 建议关闭'
                priority = 1
            elif impressions >= 200 and ctr >= 0.005 and sold_qty == 0:
                action = 'KEEP_AD'
                reason = f'展示{impressions}, CTR {ctr:.1%}, 等待转化中 (检查价格竞争力)'
                priority = 0
            else:
                action = 'KEEP_AD'
                reason = f'推广中: 展示{impressions}, 浏览{views}'
                priority = 0
        else:
            # 无广告
            if sold_qty > 0:
                # 无广告但有销售 → 有机成交
                if net_margin >= min_margin_for_ad:
                    action = 'START_AD'
                    reason = f'自然成交{sold_qty}件+利润{net_margin:.1%}充足, 开广告可进一步提升'
                    priority = 1
                else:
                    action = 'NO_CHANGE'
                    reason = f'自然成交{sold_qty}件, 利润{net_margin:.1%}不足以开广告'
                    priority = 0
            elif net_margin >= 0.12 and impressions < 50:
                # 高利润低曝光 → 建议开广告
                action = 'START_AD'
                reason = f'利润率{net_margin:.1%}远超5%广告费, 低曝光({impressions}), 强烈建议推广'
                priority = 2
            elif net_margin >= min_margin_for_ad and impressions < 200:
                action = 'START_AD'
                reason = f'利润{net_margin:.1%}可覆盖广告费, 曝光{impressions}不足, 建议推广'
                priority = 1
            elif net_margin < min_margin_for_ad:
                action = 'NO_CHANGE'
                reason = f'利润率{net_margin:.1%}不足以覆盖5%广告+2%缓冲, 先优化成本/价格'
                priority = 0
            else:
                action = 'NO_CHANGE'
                reason = f'展示{impressions}, 利润{net_margin:.1%}, 观察中'
                priority = 0

        recommendations.append({
            'sku': sku,
            'listing_id': lid,
            'title': p.get('title', ''),
            'cat_name': p.get('cat_name', ''),
            'image_url': p.get('image_url', ''),
            'selling_price': selling_price,
            'net_margin': net_margin,
            'age_days': p.get('age_days', 0),
            'impressions': impressions,
            'views': views,
            'ctr': ctr,
            'sold_qty': sold_qty,
            'revenue': revenue,
            'has_ad': has_ad,
            'bid_percentage': ad_info.get('bid_percentage', 0) if has_ad else 0,
            'campaign_id': ad_info.get('campaign_id', '') if has_ad else '',
            'campaign_name': campaign_name,
            'est_ad_cost': est_ad_cost,
            'roas': roas,
            'action': action,
            'reason': reason,
            'priority': priority,
        })

    return recommendations


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    print("=" * 60)
    print("Fetching eBay ad data...")
    print("=" * 60)

    svc = EbayAdService()
    data = svc.fetch_all_ad_data()
    save_ad_cache(data)

    print(f"\nCampaigns: {len(data['campaigns'])}")
    print(f"Promoted listings: {len(data['ads'])}")
    for c in data['campaigns']:
        if c['ad_count'] > 0:
            print(f"  {c['name']}: {c['ad_count']} ads @ {c['bid_percentage']}%")
    print(f"\nCache saved to {AD_CACHE_FILE}")
