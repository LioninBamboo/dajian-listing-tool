"""统一的改价安全守门员.

任何要把价格写出到 eBay 的代码路径 (daily 跟价 / 周一周四智能定价 /
手工改价 / MI 建议价) 调用本模块前都不会触碰 eBay API.

🛡️ 三层防线:
  1. 业务层公式 (PricingEngine.calculate_smart_price 自带 5% 安全缓冲)
  2. 业务侧软底价 (safe_floor_price, safety_margin > 0)
  3. 末端死线 + 广告自适应 (本模块) ←最后兜底

🎯 广告自适应: AD_RATE 不是钉死的. 当目标价 < 带广告死线但 >= 关广告死线时,
自动关掉该 listing 的广告 (维持竞争力但绝不亏本).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ─── DB 查询小工具 ───

def _fetch_cost_and_listing(db_path: str, sku: str) -> Tuple[Optional[float], Optional[str]]:
    """一次性取 SKU 的 total_dajian_cost 和 listing_id."""
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT cost_breakdown, listing_id FROM collected_products WHERE sku = ?",
            (sku,),
        )
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None, None
    if not row:
        return None, None
    cost = None
    if row[0]:
        try:
            cb = json.loads(row[0])
            tc = cb.get('total_dajian_cost')
            if tc and float(tc) > 0:
                cost = float(tc)
        except Exception:
            pass
    listing_id = str(row[1]) if row[1] else None
    return cost, listing_id


# ─── 主入口 ───

def precheck_price(sku: str, new_price: float, *,
                   db_path: str = 'ebay_collection.db',
                   total_cost: Optional[float] = None,
                   listing_id: Optional[str] = None,
                   allow_ad_disable: bool = True) -> Tuple[bool, str]:
    """改价前调用. 返回 (ok, reason).

    ok=False  → 调用方必须直接 return False, 不要再调 eBay API.
    ok=True   → 安全, 可以继续写出 (可能已自动关掉了广告).

    Args:
        sku: 商品 SKU
        new_price: 准备写入 eBay 的 listing price
        db_path: ebay_collection.db 路径
        total_cost: 已知的总到岸成本 (可选, None 时从 DB 读)
        listing_id: 已知的 eBay listing_id (可选, None 时从 DB 读)
        allow_ad_disable: True 时允许触底自动关广告 (推荐). False 时只做静态校验.
    """
    try:
        from src.services.pricing_engine import PricingEngine
    except Exception as e:
        logger.warning(f"⚠️ [PRICE GUARD] {sku}: PricingEngine 不可用, 放行: {e}")
        return True, "pricing_engine_unavailable"

    if total_cost is None or listing_id is None:
        db_cost, db_lid = _fetch_cost_and_listing(db_path, sku)
        if total_cost is None:
            total_cost = db_cost
        if listing_id is None:
            listing_id = db_lid

    if not total_cost or total_cost <= 0:
        # 数据缺失 → fail-open (避免业务被新增校验阻塞)
        return True, "no_cost_data"

    # 第一道: 假设广告开着的死线
    ok_with_ad, reason_with_ad = PricingEngine.assert_safe_price(
        price=new_price, total_cost=total_cost, safety_margin=0.0,
    )
    if ok_with_ad:
        return True, "ok"

    # 第二道: 假设广告关掉的死线 (更低)
    ok_no_ad, reason_no_ad = PricingEngine.assert_safe_price(
        price=new_price, total_cost=total_cost,
        safety_margin=0.0, ad_rate=0.0,
    )
    if not ok_no_ad:
        # 关广告也救不了
        logger.error(
            f"🚫 [PRICE GUARD] {sku}: 拒绝改价 - 即使关广告仍亏本. "
            f"with_ad: {reason_with_ad}; no_ad: {reason_no_ad}"
        )
        return False, f"unsafe_even_without_ad: {reason_no_ad}"

    if not allow_ad_disable:
        logger.error(
            f"🚫 [PRICE GUARD] {sku}: 拒绝改价 - 价格 < 带广告死线 "
            f"且不允许自动关广告. {reason_with_ad}"
        )
        return False, f"unsafe_with_ad_no_disable: {reason_with_ad}"

    # 第三道: 尝试关广告
    disabled, ad_status = _try_disable_ad(sku, listing_id, reason_with_ad)
    if disabled:
        return True, f"ok_after_ad_disabled (was {reason_with_ad})"
    if ad_status == 'no_ad':
        # 本就没广告 → 本来就没付广告费 → 价格安全
        logger.info(
            f"✅ [PRICE GUARD] {sku}: 价格 < 带广告死线但本就无广告, 放行. ({reason_with_ad})"
        )
        return True, "ok_no_ad_to_disable"
    # ad_status == 'failed' or 'unknown': 仍可能在打广告 → 保守拒绝
    logger.error(
        f"🚫 [PRICE GUARD] {sku}: 拒绝改价 - 价格 < 带广告死线且关广告失败. {reason_with_ad}"
    )
    return False, f"ad_disable_failed: {reason_with_ad}"


def _try_disable_ad(sku: str, listing_id: Optional[str], reason: str) -> Tuple[bool, str]:
    """尝试关掉 listing 的广告.

    Returns:
        (disabled, status):
          (True, 'disabled')   → 成功关掉了
          (False, 'no_ad')     → 本来就没在打广告
          (False, 'failed')    → 调用 eBay 失败
          (False, 'unknown')   → 没有 listing_id, 无法操作
    """
    if not listing_id:
        return False, 'unknown'
    try:
        from src.services.ebay_ad_service import EbayAdService
        ad_svc = EbayAdService()
        ad_info = ad_svc.find_ad_for_listing(listing_id)
        if not ad_info:
            logger.info(
                f"💡 [AD AUTO-OFF] {sku} (listing={listing_id}): 本就无广告. ({reason})"
            )
            return False, 'no_ad'
        cid = ad_info['campaign_id']
        ad_id = ad_info['ad_id']
        # delete_ad 接收 (campaign_id, listing_id) 并返回 dict
        result = ad_svc.delete_ad(cid, listing_id)
        if isinstance(result, dict) and result.get('success'):
            logger.warning(
                f"🛑 [AD AUTO-OFF] {sku} (listing={listing_id}): 触底自动关广告 "
                f"(campaign={cid}, ad={ad_id}, 原 bid={ad_info.get('bid_percentage')}%). "
                f"原因: {reason}"
            )
            # P4: 累计关广告次数, 达到阈值自动加入广告黑名单
            try:
                from src.services import ad_blacklist
                ad_blacklist.bump_off_count(sku, reason_hint='auto_off')
            except Exception as _bl_exc:
                logger.warning(f"广告黑名单累计失败 (非阻塞): {_bl_exc}")
            return True, 'disabled'
        err = result.get('error', '?') if isinstance(result, dict) else str(result)
        logger.error(f"❌ [AD AUTO-OFF] {sku}: delete_ad 失败 - {err}")
        return False, 'failed'
    except Exception as e:
        logger.error(f"❌ [AD AUTO-OFF] {sku}: 异常 {e}")
        return False, 'failed'
