"""统一的改价安全守门员.

任何要把价格写出到 eBay 的代码路径 (daily 跟价 / 周一周四智能定价 /
手工改价 / MI 建议价) 调用本模块前都不会触碰 eBay API.

🛡️ 三层防线:
  1. 业务层公式 (PricingEngine.calculate_smart_price, 售价净利 ≥ MIN_NET_MARGIN)
  2. 业务侧软底价 (PricingEngine.net_margin_floor_price, 同一售价口径)
  3. 末端守卫 + 广告决策 (本模块) ←最后兜底

🎯 广告决策: 目标价带广告低于 10% 净利时标记 pause_ad, 并按关广告重算。
关广告后仍不足 10% 则把价格抬到 10% 净利底, 不再拒绝改价。
真正在 eBay 上关广告必须显式 opt-in (默认关); 未真正关广告时地板按带广告费计算。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

EXECUTE_AD_PAUSE_ENV = "PRICE_GUARD_EXECUTE_AD_PAUSE"


def _truthy_env(name: str) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def execute_ad_pause_enabled(explicit: Optional[bool] = None) -> bool:
    """Live eBay ad-pause is opt-in. Default OFF."""
    if explicit is not None:
        return bool(explicit)
    return _truthy_env(EXECUTE_AD_PAUSE_ENV)


@dataclass
class PriceGuardResult:
    """Guard decision. Unpacks as ``(ok, reason)`` for existing callers."""

    ok: bool
    reason: str
    pause_ad: bool = False
    pause_ad_candidate: bool = False
    ad_paused: bool = False
    adjusted_price: Optional[float] = None
    requested_price: Optional[float] = None
    floor_with_ad: Optional[float] = None
    floor_no_ad: Optional[float] = None
    sku: str = ""

    def __iter__(self):
        yield self.ok
        yield self.reason

    def __getitem__(self, index: int):
        return (self.ok, self.reason)[index]

    def __len__(self) -> int:
        return 2

    def as_report_fields(self) -> dict:
        return {
            "sku": self.sku,
            "ok": self.ok,
            "reason": self.reason,
            "pause_ad": self.pause_ad,
            "pause_ad_candidate": self.pause_ad_candidate,
            "ad_paused": self.ad_paused,
            "requested_price": self.requested_price,
            "adjusted_price": self.adjusted_price,
            "floor_with_ad": self.floor_with_ad,
            "floor_no_ad": self.floor_no_ad,
        }

# ─── DB 查询小工具 ───

def _fetch_cost_and_listing(db_path: str, sku: str) -> Tuple[Optional[float], Optional[str]]:
    """一次性取 SKU 的到岸成本 (含运费回填) 和 listing_id."""
    try:
        from src.services.pricing_engine import PricingEngine
    except Exception:
        PricingEngine = None  # type: ignore
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT cost_breakdown, listing_id, shipping, specs, attributes "
                "FROM collected_products WHERE sku = ?",
                (sku,),
            )
        except sqlite3.OperationalError:
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
            shipping = float(row[2] or 0) if len(row) > 2 else 0.0
            specs = {}
            attrs = {}
            if len(row) > 3 and row[3]:
                try:
                    specs = json.loads(row[3]) if isinstance(row[3], str) else (row[3] or {})
                except Exception:
                    specs = {}
            if len(row) > 4 and row[4]:
                try:
                    attrs = json.loads(row[4]) if isinstance(row[4], str) else (row[4] or {})
                except Exception:
                    attrs = {}
            if PricingEngine is not None and hasattr(PricingEngine, "resolve_landed_cost"):
                landed = PricingEngine.resolve_landed_cost(
                    cb,
                    shipping_fallback=shipping,
                    specs=specs if isinstance(specs, dict) else {},
                    attributes=attrs if isinstance(attrs, dict) else {},
                )
                if landed and float(landed) > 0:
                    cost = float(landed)
            if cost is None:
                tc = cb.get('total_dajian_cost')
                if tc and float(tc) > 0:
                    cost = float(tc)
        except Exception:
            pass
    listing_id = str(row[1]) if row[1] else None
    return cost, listing_id


def reprice_write_channel(listing_id: Optional[str] = None, profile=None) -> str:
    """Return ``trading`` or ``inventory`` for one live listing's price write.

    Gated on ``store_profile.listing_channel``. Furniture/main defaults stay
    on Inventory offer PUT. Motors/Trading stores write through
    ``ReviseFixedPriceItem``. ``listing_id`` is part of the per-listing
    contract (sub-stores may override locally) but shared defaults are
    store-level only.
    """
    if profile is None:
        from src.utils.store_profile import get_store_profile

        profile = get_store_profile()
    channel = str(getattr(profile, "listing_channel", "") or "").strip().lower()
    if channel != "trading":
        return "inventory"
    extra = str(os.environ.get("REPRICE_INVENTORY_LISTING_IDS", "") or "")
    inventory_ids = {item.strip() for item in extra.split(",") if item.strip()}
    if str(listing_id or "").strip() in inventory_ids:
        return "inventory"
    return "trading"


# ─── 主入口 ───

def precheck_price(sku: str, new_price: float, *,
                   db_path: str = 'ebay_collection.db',
                   total_cost: Optional[float] = None,
                   listing_id: Optional[str] = None,
                   allow_ad_disable: bool = True,
                   execute_ad_pause: Optional[bool] = None,
                   dry_run: bool = False,
                   currency: Any = "USD") -> PriceGuardResult:
    """改价前调用. 返回 PriceGuardResult (仍可解包为 ``ok, reason``).

    ok=False  → 调用方必须直接 return False, 不要再调 eBay API.
    ok=True   → 写出 ``adjusted_price`` (可能已抬到 10% 净利底).

    真正调用 eBay 关广告仅当 ``execute_ad_pause`` / env
    ``PRICE_GUARD_EXECUTE_AD_PAUSE`` 为真, 且 ``allow_ad_disable`` 且非 dry-run.
    未真正关广告时地板按带广告费计算, 保证 10% 净利.
    """
    sku_key = "" if sku is None else str(sku)
    try:
        from src.services.pricing_engine import PricingEngine, parse_money
    except Exception as e:
        logger.error(f"🚫 [PRICE GUARD] {sku_key}: PricingEngine 不可用, 拒绝改价: {e}")
        return PriceGuardResult(
            ok=False,
            reason="pricing_engine_unavailable",
            sku=sku_key,
            requested_price=_safe_float(new_price),
        )

    currency_err = PricingEngine.assert_usd_currency(currency)
    if currency_err:
        logger.error(f"🚫 [PRICE GUARD] {sku_key}: {currency_err}")
        return PriceGuardResult(
            ok=False,
            reason=currency_err,
            sku=sku_key,
            requested_price=_safe_float(new_price),
        )

    if total_cost is None or listing_id is None:
        db_cost, db_lid = _fetch_cost_and_listing(db_path, sku_key)
        if total_cost is None:
            total_cost = db_cost
        if listing_id is None:
            listing_id = db_lid

    cost = parse_money(total_cost)
    listing = parse_money(new_price)
    if cost is None:
        logger.error(
            f"🚫 [PRICE GUARD] {sku_key}: 拒绝改价 - 成本未知或非法 "
            f"(cost={total_cost!r})"
        )
        return PriceGuardResult(
            ok=False,
            reason="unknown_or_invalid_cost",
            sku=sku_key,
            requested_price=_safe_float(new_price),
        )
    if listing is None:
        logger.error(
            f"🚫 [PRICE GUARD] {sku_key}: 拒绝改价 - 价格未知或非法 "
            f"(price={new_price!r})"
        )
        return PriceGuardResult(
            ok=False,
            reason="invalid_price",
            sku=sku_key,
        )

    with_ad = PricingEngine.compute_net_margin_floor(cost)
    no_ad = PricingEngine.compute_net_margin_floor(cost, ad_rate=0.0)
    if not with_ad["ok"] or not no_ad["ok"]:
        reason = with_ad["reason"] if not with_ad["ok"] else no_ad["reason"]
        logger.error(f"🚫 [PRICE GUARD] {sku_key}: 拒绝改价 - 无法计算净利底 ({reason})")
        return PriceGuardResult(
            ok=False,
            reason=reason,
            sku=sku_key,
            requested_price=float(listing),
        )

    floor_with_ad = float(with_ad["price"])
    floor_no_ad = float(no_ad["price"])
    requested = float(listing)
    min_margin = PricingEngine.min_net_margin()
    margin_with_ad = PricingEngine.net_margin_at_price(listing, cost)
    margin_no_ad = PricingEngine.net_margin_at_price(listing, cost, ad_rate=0.0)
    if margin_with_ad is None or margin_no_ad is None:
        logger.error(f"🚫 [PRICE GUARD] {sku_key}: 拒绝改价 - 费率数据缺失")
        return PriceGuardResult(
            ok=False,
            reason="fee_data_invalid",
            sku=sku_key,
            requested_price=requested,
            floor_with_ad=floor_with_ad,
            floor_no_ad=floor_no_ad,
        )

    want_pause = margin_with_ad < min_margin
    may_execute = (
        bool(allow_ad_disable)
        and not dry_run
        and execute_ad_pause_enabled(execute_ad_pause)
    )

    if margin_with_ad >= min_margin:
        logger.info(f"✅ [PRICE GUARD] {sku_key}: 带广告净利达标, 放行 ${requested:.2f}")
        return PriceGuardResult(
            ok=True,
            reason="ok",
            pause_ad=False,
            pause_ad_candidate=False,
            adjusted_price=requested,
            requested_price=requested,
            floor_with_ad=floor_with_ad,
            floor_no_ad=floor_no_ad,
            sku=sku_key,
        )

    if margin_no_ad >= min_margin:
        if may_execute:
            disabled, ad_status = _try_disable_ad(
                sku_key, listing_id, "below_with_ad_net_floor"
            )
            if disabled or ad_status == "no_ad":
                reason = (
                    "ok_after_ad_disabled"
                    if disabled
                    else "ok_no_ad_to_disable"
                )
                logger.info(
                    f"✅ [PRICE GUARD] {sku_key}: 关广告后净利达标, "
                    f"保持目标价 ${requested:.2f} ({reason})"
                )
                return PriceGuardResult(
                    ok=True,
                    reason=reason,
                    pause_ad=True,
                    pause_ad_candidate=True,
                    ad_paused=bool(disabled),
                    adjusted_price=requested,
                    requested_price=requested,
                    floor_with_ad=floor_with_ad,
                    floor_no_ad=floor_no_ad,
                    sku=sku_key,
                )
            logger.warning(
                f"⚠️ [PRICE GUARD] {sku_key}: 关广告失败, 抬到带广告 10% 净利底 "
                f"${floor_with_ad:.2f} (原目标 ${requested:.2f})"
            )
            return PriceGuardResult(
                ok=True,
                reason="raised_to_with_ad_floor_ad_pause_failed",
                pause_ad=True,
                pause_ad_candidate=True,
                ad_paused=False,
                adjusted_price=floor_with_ad,
                requested_price=requested,
                floor_with_ad=floor_with_ad,
                floor_no_ad=floor_no_ad,
                sku=sku_key,
            )
        logger.info(
            f"📈 [PRICE GUARD] {sku_key}: pause_ad 候选, 未真正关广告, "
            f"按带广告地板从 ${requested:.2f} 抬到 ${floor_with_ad:.2f}"
        )
        return PriceGuardResult(
            ok=True,
            reason="raised_to_with_ad_floor",
            pause_ad=True,
            pause_ad_candidate=True,
            ad_paused=False,
            adjusted_price=floor_with_ad,
            requested_price=requested,
            floor_with_ad=floor_with_ad,
            floor_no_ad=floor_no_ad,
            sku=sku_key,
        )

    # Ads-off still below 10%: raise instead of refusing.
    if may_execute:
        disabled, ad_status = _try_disable_ad(
            sku_key, listing_id, "below_no_ad_net_floor"
        )
        if disabled or ad_status == "no_ad":
            write_price = floor_no_ad
            logger.info(
                f"📈 [PRICE GUARD] {sku_key}: 关广告后仍低于 10% 净利, "
                f"从 ${requested:.2f} 抬到关广告地板 ${write_price:.2f}"
            )
            return PriceGuardResult(
                ok=True,
                reason="raised_to_net_floor",
                pause_ad=True,
                pause_ad_candidate=True,
                ad_paused=bool(disabled),
                adjusted_price=write_price,
                requested_price=requested,
                floor_with_ad=floor_with_ad,
                floor_no_ad=floor_no_ad,
                sku=sku_key,
            )
        logger.warning(
            f"⚠️ [PRICE GUARD] {sku_key}: 关广告失败且目标低于关广告地板, "
            f"抬到带广告 10% 净利底 ${floor_with_ad:.2f}"
        )
        return PriceGuardResult(
            ok=True,
            reason="raised_to_with_ad_floor_ad_pause_failed",
            pause_ad=True,
            pause_ad_candidate=True,
            ad_paused=False,
            adjusted_price=floor_with_ad,
            requested_price=requested,
            floor_with_ad=floor_with_ad,
            floor_no_ad=floor_no_ad,
            sku=sku_key,
        )

    logger.info(
        f"📈 [PRICE GUARD] {sku_key}: 即使关广告仍低于 10% 净利, "
        f"未真正关广告, 从 ${requested:.2f} 抬到带广告地板 ${floor_with_ad:.2f}"
    )
    return PriceGuardResult(
        ok=True,
        reason="raised_to_net_floor",
        pause_ad=True,
        pause_ad_candidate=True,
        ad_paused=False,
        adjusted_price=floor_with_ad,
        requested_price=requested,
        floor_with_ad=floor_with_ad,
        floor_no_ad=floor_no_ad,
        sku=sku_key,
    )


def _safe_float(value: Any) -> Optional[float]:
    try:
        from src.services.pricing_engine import parse_money
        parsed = parse_money(value)
        return float(parsed) if parsed is not None else None
    except Exception:
        return None


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
