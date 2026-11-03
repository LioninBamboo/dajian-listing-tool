from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Net profit / sale price. Env MIN_NET_MARGIN_PCT overrides (fraction, e.g. 0.12).
MIN_NET_MARGIN = Decimal("0.10")
_CENTS = Decimal("0.01")


def parse_money(value: Any) -> Optional[Decimal]:
    """Parse a USD amount. None / 0 / negative / unreadable → None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            text = str(value).strip()
            if not text:
                return None
            amount = Decimal(text)
        except (InvalidOperation, ValueError, TypeError):
            return None
    if not amount.is_finite() or amount <= 0:
        return None
    return amount


class PricingEngine:
    """
    GIGA/Dajian procurement + storefront pricing calculator.

    Procurement cost is ALWAYS based on the GIGA order (product + shipping),
    never on eBay/Wayfair selling price.
    """

    # --- GIGA order-side constants (applied to GIGA order amount) ---
    RETURN_INSURANCE_RATE = Decimal("0.02")      # 2% 退货保障
    LOGISTICS_INSURANCE_EXPRESS = Decimal("0.032") # 3.2% 快递物流保障
    LOGISTICS_INSURANCE_FREIGHT = Decimal("0.05")  # 5% 卡车/大件物流保障
    PAYMENT_FEE_RATE = Decimal("0.0083")         # 0.83% 支付宝支付手续费 (GIGA 付款)
    # Compatibility alias — same as Alipay rate on GIGA pay.
    ALIPAY_FEE_RATE = PAYMENT_FEE_RATE
    # Wayfair Net-30 remittance fee (sales settlement, not GIGA product price).
    WAYFAIR_NET30_REMIT_RATE = Decimal("0.02")   # 2% 30天汇款

    # eBay Costs — FVF 13.6% + APAC international 1.3% of the tax-inclusive
    # sale total + $0.40/order. Live US-bound orders on this account print
    # FVF ~12.7% + intl 1.3% (=14.0%); we keep 13.6%+1.3% so the 10% floor
    # is slightly conservative. Listing floors pad a 10% destination tax on
    # the fee base so net still ≥ 10% of cost when collected tax ≤ 10%.
    # Order PnL uses collected tax, not the pad.
    EBAY_FEE_RATE = Decimal("0.136")            # 13.6% FVF
    INTERNATIONAL_FEE_RATE = Decimal("0.013")   # 1.3% APAC international
    LISTING_TAX_PAD = Decimal("0.10")           # 10% tax pad at list time
    AD_RATE = Decimal("0.05")                   # 5.00%
    FIXED_FEE = Decimal("0.40")                 # $0.40

    # Store Discount (长期店铺折扣)
    STORE_DISCOUNT_RATE = Decimal("0.05")  # 5% 买家折扣

    # 写出 / 智能定价 / 价格守卫的统一净利底 = 净利润 / 标价 ≥ 10%。
    # MIN_NET_MARGIN_ON_COST 是同值别名, 留给 CRO / 开广告的成本加价口径调用方.
    MIN_NET_MARGIN = MIN_NET_MARGIN
    MIN_NET_MARGIN_ON_COST = MIN_NET_MARGIN

    @staticmethod
    def min_net_margin() -> Decimal:
        """Sale-price net margin floor. Env ``MIN_NET_MARGIN_PCT`` overrides.

        Expected override is a fraction (``0.12`` = 12%). Invalid / empty /
        out-of-range values fall back to 0.10 with a warning. Does not read
        or write ``.env``.
        """
        raw = os.environ.get("MIN_NET_MARGIN_PCT")
        if raw is None:
            return PricingEngine.MIN_NET_MARGIN
        text = str(raw).strip()
        if not text:
            return PricingEngine.MIN_NET_MARGIN
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError, TypeError):
            logger.warning(
                "Invalid MIN_NET_MARGIN_PCT=%r; using default %s",
                raw,
                PricingEngine.MIN_NET_MARGIN,
            )
            return PricingEngine.MIN_NET_MARGIN
        if not value.is_finite() or value < 0 or value >= Decimal("0.70"):
            logger.warning(
                "Out-of-range MIN_NET_MARGIN_PCT=%r; using default %s",
                raw,
                PricingEngine.MIN_NET_MARGIN,
            )
            return PricingEngine.MIN_NET_MARGIN
        return value

    @staticmethod
    def assert_usd_currency(currency: Any = "USD") -> Optional[str]:
        """Return a refusal reason when currency is present and not USD."""
        if currency is None:
            return None
        text = str(currency).strip()
        if not text:
            return None
        if text.upper() != "USD":
            return f"unsupported_currency:{text}"
        return None

    @staticmethod
    def calculate_dajian_cost(
        product_price: float,
        shipping_cost: float,
        is_oversize: bool = False,
        *,
        include_alipay_fee: bool = True,
        include_wayfair_net30_fee: bool = False,
    ) -> dict:
        """Calculate total procurement / all-in cost from a GIGA order.

        Base is always the GIGA order (product + shipping), NEVER the listing
        sell price.

        Components:
          1) GIGA order base = product_price + shipping_cost
          2) Insurance (purchased on order base):
               - return insurance 2%
               - logistics insurance 3.2% express / 5% freight(oversize)
          3) Alipay payment fee 0.83% on (base + insurance) when paying GIGA
          4) Optional Wayfair Net-30 remittance fee 2% on (base + insurance)
             — use for Wayfair-channel economics / 30-day remittance orders

        Args:
            product_price: GIGA order product amount (not eBay sell price)
            shipping_cost: GIGA order shipping amount
            is_oversize: use freight logistics insurance rate
            include_alipay_fee: apply 0.83% Alipay fee (default True)
            include_wayfair_net30_fee: apply 2% Wayfair Net-30 remittance fee
        """
        try:
            product = Decimal(str(product_price or 0))
        except Exception:
            product = Decimal("0")
        try:
            shipping = Decimal(str(shipping_cost or 0))
        except Exception:
            shipping = Decimal("0")
        if product < 0 or shipping < 0:
            raise ValueError(
                f"GIGA order amounts must be non-negative, got product={product_price!r} shipping={shipping_cost!r}"
            )

        # 1) GIGA order base — not sell price
        base_cost = product + shipping

        # 2) Insurance purchased on the GIGA order
        return_ins = base_cost * PricingEngine.RETURN_INSURANCE_RATE
        logistics_rate = (
            PricingEngine.LOGISTICS_INSURANCE_FREIGHT
            if is_oversize
            else PricingEngine.LOGISTICS_INSURANCE_EXPRESS
        )
        logistics_ins = base_cost * logistics_rate
        insured_subtotal = base_cost + return_ins + logistics_ins

        # 3) Alipay 0.83% — fee when paying the GIGA order
        alipay_fee = (
            insured_subtotal * PricingEngine.ALIPAY_FEE_RATE
            if include_alipay_fee
            else Decimal("0")
        )

        # 4) Wayfair Net-30 remittance 2% (optional; sales settlement leg)
        wayfair_remit_fee = (
            insured_subtotal * PricingEngine.WAYFAIR_NET30_REMIT_RATE
            if include_wayfair_net30_fee
            else Decimal("0")
        )

        # Legacy field name payment_fee = Alipay only (keeps older callers stable)
        payment_fee = alipay_fee
        total_dajian_cost = insured_subtotal + alipay_fee + wayfair_remit_fee

        return {
            "product_price": float(product),
            "shipping_cost": float(shipping),
            "base_cost": float(base_cost),
            "giga_order_base": float(base_cost),
            "return_insurance": float(return_ins),
            "logistics_insurance": float(logistics_ins),
            "logistics_insurance_rate": float(logistics_rate),
            "insured_subtotal": float(insured_subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "payment_fee": float(payment_fee),
            "alipay_fee": float(alipay_fee.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "alipay_fee_rate": float(PricingEngine.ALIPAY_FEE_RATE) if include_alipay_fee else 0.0,
            "wayfair_net30_fee": float(wayfair_remit_fee.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "wayfair_net30_fee_rate": float(PricingEngine.WAYFAIR_NET30_REMIT_RATE) if include_wayfair_net30_fee else 0.0,
            "include_alipay_fee": bool(include_alipay_fee),
            "include_wayfair_net30_fee": bool(include_wayfair_net30_fee),
            "cost_basis": "giga_order",
            "total_dajian_cost": float(total_dajian_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        }

    @staticmethod
    def resolve_landed_cost(
        cost_breakdown: dict | None = None,
        *,
        shipping_fallback: float = 0.0,
        specs: dict | None = None,
        attributes: dict | None = None,
    ) -> float | None:
        """Rebuild total landed cost when freight was omitted from the snapshot.

        Freight is the higher of ``cost_breakdown.shipping_cost`` and
        ``shipping_fallback`` (usually ``collected_products.shipping``). When
        ``product_price`` is present the full Dajian model is recalculated.
        Otherwise the stored ``total_dajian_cost`` is kept.
        """
        breakdown = cost_breakdown or {}
        try:
            product = float(breakdown.get("product_price") or 0)
        except (TypeError, ValueError):
            product = 0.0
        try:
            stored_ship = float(breakdown.get("shipping_cost") or 0)
        except (TypeError, ValueError):
            stored_ship = 0.0
        try:
            fallback_ship = float(shipping_fallback or 0)
        except (TypeError, ValueError):
            fallback_ship = 0.0
        shipping = max(stored_ship, fallback_ship)
        oversize = "Dimensions" in (specs or {}) or "Dimensions" in (attributes or {})
        if product > 0:
            return PricingEngine.calculate_dajian_cost(product, shipping, oversize)[
                "total_dajian_cost"
            ]
        try:
            stored_total = float(breakdown.get("total_dajian_cost") or 0)
        except (TypeError, ValueError):
            stored_total = 0.0
        return stored_total if stored_total > 0 else None


    # ════════════════════════════════════════════════════════════════════
    # 死线 / 安全底 / 守门员 — 所有改价入口必须遵守的统一防线
    # ════════════════════════════════════════════════════════════════════
    #
    # 公式推导 (5% 折扣 + 13.6% FVF + 1.3% 国际费 + 广告 + $0.40, 刊登 10% 税垫):
    #   买家货款 = listing_price × (1 - discount)
    #   费用税基 = 货款 × (1 + tax_pad)
    #   卖家净收 = 货款 − 费用税基 × (fvf + intl + ad) − fixed_fee
    #            = listing_price × discount_denom − fixed_fee
    #   利润    = 净收 - cost
    #
    # 死线 (margin=0): 净收 = cost  →  listing_price = (cost + fixed) / discount_denom
    # 安全底 (margin=s): 净收 = cost × (1+s)  →  listing_price = (cost×(1+s) + fixed) / discount_denom

    @staticmethod
    def marketplace_fee_rate() -> Decimal:
        """FVF + international fee (excludes ads and the listing tax pad)."""
        return PricingEngine.EBAY_FEE_RATE + PricingEngine.INTERNATIONAL_FEE_RATE

    @staticmethod
    def _variable_take(ad_rate: float = None) -> Decimal:
        ad = PricingEngine.AD_RATE if ad_rate is None else Decimal(str(ad_rate))
        return PricingEngine.marketplace_fee_rate() + ad

    @staticmethod
    def _discount_denom(ad_rate: float = None) -> Decimal:
        """(1 - 折扣) × (1 - (FVF+国际费+ad) × (1+税垫)).

        ad_rate=None  → 用 PricingEngine.AD_RATE (5%, 默认假设广告开着)
        ad_rate=0     → 假设广告关掉, 死线下降 (扩大让价空间)
        ad_rate=0.04  → 假设降低竞价率
        """
        ad = PricingEngine.AD_RATE if ad_rate is None else Decimal(str(ad_rate))
        take = PricingEngine._variable_take(ad_rate)
        padded = take * (Decimal("1") + PricingEngine.LISTING_TAX_PAD)
        if ad < 0 or padded >= Decimal("1"):
            cap = float(
                (Decimal("1") / (Decimal("1") + PricingEngine.LISTING_TAX_PAD))
                - PricingEngine.marketplace_fee_rate()
            )
            raise ValueError(f"ad_rate must be in [0, {cap}), got {ad_rate}")
        return (Decimal("1") - PricingEngine.STORE_DISCOUNT_RATE) * (
            Decimal("1") - padded
        )

    @staticmethod
    def net_profit_and_margin(
        price: Any,
        total_cost: Any,
        ad_rate: float = None,
        *,
        currency: Any = "USD",
    ) -> tuple[Optional[Decimal], Optional[Decimal], str]:
        """Net profit and sale-price margin using the existing fee model.

        net_profit = listing_price × discount_denom − fixed_fee − cost
        margin     = net_profit / listing_price

        Returns (net_profit, margin, reason). reason is ``ok`` on success.
        """
        currency_err = PricingEngine.assert_usd_currency(currency)
        if currency_err:
            return None, None, currency_err
        cost = parse_money(total_cost)
        listing = parse_money(price)
        if cost is None:
            return None, None, "unknown_or_invalid_cost"
        if listing is None:
            return None, None, "invalid_price"
        try:
            denom = PricingEngine._discount_denom(ad_rate)
        except ValueError as exc:
            return None, None, f"fee_data_invalid:{exc}"
        net_profit = listing * denom - PricingEngine.FIXED_FEE - cost
        margin = net_profit / listing
        return net_profit, margin, "ok"

    @staticmethod
    def net_margin_at_price(
        price: Any,
        total_cost: Any,
        ad_rate: float = None,
        *,
        currency: Any = "USD",
    ) -> Optional[Decimal]:
        """Sale-price net margin, or None when price/cost/fees cannot be used."""
        _profit, margin, _reason = PricingEngine.net_profit_and_margin(
            price, total_cost, ad_rate, currency=currency
        )
        return margin

    @staticmethod
    def net_margin_floor_price(
        total_cost: Any,
        ad_rate: float = None,
        min_margin: Any = None,
        *,
        currency: Any = "USD",
    ) -> float:
        """Lowest listing price with net profit / sale price ≥ min_margin.

        Algebra: listing = (cost + fixed) / (discount_denom − min_margin).
        Rounds UP to the cent so rounding cannot drop the margin below the
        floor. Single source of truth for smart reprice, price guard, publish
        pricing, and inventory-sync floor checks.

        Raises:
            ValueError: unknown/invalid cost, unsupported currency, or fee
                data that leaves no residual for the requested margin.
        """
        result = PricingEngine.compute_net_margin_floor(
            total_cost, ad_rate=ad_rate, min_margin=min_margin, currency=currency
        )
        if not result["ok"]:
            raise ValueError(result["reason"])
        return float(result["price"])

    @staticmethod
    def compute_net_margin_floor(
        total_cost: Any,
        ad_rate: float = None,
        min_margin: Any = None,
        *,
        currency: Any = "USD",
    ) -> dict:
        """Floor result dict: ``ok``, ``price``, ``reason``, ``margin``, ``min_margin``."""
        currency_err = PricingEngine.assert_usd_currency(currency)
        if currency_err:
            return {
                "ok": False,
                "price": None,
                "reason": currency_err,
                "margin": None,
                "min_margin": None,
            }
        cost = parse_money(total_cost)
        if cost is None:
            return {
                "ok": False,
                "price": None,
                "reason": "unknown_or_invalid_cost",
                "margin": None,
                "min_margin": None,
            }
        if min_margin is None:
            target = PricingEngine.min_net_margin()
        else:
            try:
                if isinstance(min_margin, bool):
                    raise InvalidOperation("bool")
                target = Decimal(str(min_margin).strip())
            except (InvalidOperation, ValueError, TypeError):
                return {
                    "ok": False,
                    "price": None,
                    "reason": "invalid_min_margin",
                    "margin": None,
                    "min_margin": None,
                }
        if not target.is_finite() or target < 0:
            return {
                "ok": False,
                "price": None,
                "reason": "invalid_min_margin",
                "margin": None,
                "min_margin": None,
            }
        try:
            denom = PricingEngine._discount_denom(ad_rate)
        except ValueError as exc:
            return {
                "ok": False,
                "price": None,
                "reason": f"fee_data_invalid:{exc}",
                "margin": None,
                "min_margin": float(target),
            }
        leftover = denom - target
        if leftover <= 0:
            return {
                "ok": False,
                "price": None,
                "reason": (
                    f"fee_data_invalid:margin {target} + fees leave no residual "
                    f"(denom={denom})"
                ),
                "margin": None,
                "min_margin": float(target),
            }
        raw = (cost + PricingEngine.FIXED_FEE) / leftover
        floor = raw.quantize(_CENTS, rounding=ROUND_CEILING)
        _profit, margin, margin_reason = PricingEngine.net_profit_and_margin(
            floor, cost, ad_rate, currency=currency
        )
        if margin is None:
            return {
                "ok": False,
                "price": None,
                "reason": margin_reason,
                "margin": None,
                "min_margin": float(target),
            }
        return {
            "ok": True,
            "price": floor,
            "reason": "ok",
            "margin": margin,
            "min_margin": float(target),
        }

    @staticmethod
    def absolute_floor_price(total_cost: float, ad_rate: float = None) -> float:
        """
        ⚠️ 死线: listing price 在此值以下卖一定亏损 (含所有费率与折扣).

        Args:
            total_cost: 总到岸成本
            ad_rate: 广告费率, None 表示用 5% 默认值 (假设广告开着).
                     传 0 表示假设广告已关 → 死线更低 (扩大让价空间).
        """
        if total_cost is None or total_cost <= 0:
            return 0.0
        cost = Decimal(str(total_cost))
        floor = (cost + PricingEngine.FIXED_FEE) / PricingEngine._discount_denom(ad_rate)
        return float(floor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @staticmethod
    def safe_floor_price(total_cost: float, safety_margin: float = 0.05,
                         ad_rate: float = None) -> float:
        """
        软底价: 死线 + safety_margin 净利润缓冲.

        Args:
            total_cost: 总到岸成本
            safety_margin: 净利润缓冲 (相对于成本)
            ad_rate: 广告费率, None=用 5% 默认值, 0=假设广告关掉
        """
        if total_cost is None or total_cost <= 0:
            return 0.0
        if safety_margin < 0:
            raise ValueError(f"safety_margin must be ≥ 0, got {safety_margin}")
        cost = Decimal(str(total_cost))
        safety = Decimal(str(safety_margin))
        floor = (cost * (Decimal("1") + safety) + PricingEngine.FIXED_FEE) / PricingEngine._discount_denom(ad_rate)
        return float(floor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @staticmethod
    def assert_safe_price(price: float, total_cost: float, *,
                          safety_margin: float = 0.0,
                          ad_rate: float = None,
                          tolerance: float = 0.01) -> tuple:
        """
        守门员: 价格安全检查. 任何 update_ebay_price / publish 类入口都应在写出前调用.

        Args:
            price: 准备写入 eBay 的 listing price
            total_cost: 该 SKU 的总到岸成本
            safety_margin: 0 表示死线 (零利润), 默认 0
            ad_rate: 广告费率, None=用 5% 默认 (广告开着), 0=广告已关 (死线更低)
            tolerance: 容差 (默认 1 分钱, 抵消 Decimal 舍入)

        Returns:
            (ok: bool, reason: str)
        """
        if total_cost is None or total_cost <= 0:
            return True, "no_cost_data"  # 数据不全 → 不阻拦, 但调用方应自行处理
        if price is None or price <= 0:
            return False, f"invalid price={price}"
        floor = PricingEngine.safe_floor_price(total_cost, safety_margin, ad_rate=ad_rate)
        if price + tolerance < floor:
            shortfall = floor - price
            ad_label = (
                "ad-off" if ad_rate is not None and float(ad_rate) == 0
                else f"ad={float(ad_rate)*100:.1f}%" if ad_rate is not None
                else "ad=5%"
            )
            return False, (
                f"⛔ price ${price:.2f} below "
                f"{'safe floor' if safety_margin > 0 else 'BREAK-EVEN floor'} "
                f"${floor:.2f} (cost=${total_cost:.2f}, "
                f"safety={safety_margin*100:.0f}%, {ad_label}, shortfall=${shortfall:.2f})"
            )
        return True, "ok"

    @staticmethod
    def required_ad_rate_for(price: float, total_cost: float,
                             safety_margin: float = 0.0) -> float:
        """
        给定目标价和成本, 反算可承受的"最大广告费率".

        如果返回值 < 当前 AD_RATE (5%) → 该价位不能开 5% 广告, 否则会亏.
        如果返回值 < 0 → 即使关掉广告也救不了, 价格本身就亏.

        Args:
            price: listing price (扣折扣前)
            total_cost: 总到岸成本
            safety_margin: 期望的净利润率缓冲 (相对成本)

        Returns:
            最大可承受 ad_rate (Decimal float). 0 表示只能关广告, 负数表示无解.
        """
        if total_cost is None or total_cost <= 0 or price is None or price <= 0:
            return 0.0
        cost = Decimal(str(total_cost))
        p = Decimal(str(price))
        safety = Decimal(str(safety_margin))
        discount = Decimal("1") - PricingEngine.STORE_DISCOUNT_RATE
        merch = p * discount
        fee_base = merch * (Decimal("1") + PricingEngine.LISTING_TAX_PAD)
        if merch <= 0 or fee_base <= 0:
            return -1.0
        target = cost * (Decimal("1") + safety) + PricingEngine.FIXED_FEE
        # merch - fee_base * (fvf+intl+ad) - fixed >= cost*(1+safety)
        ad = (merch - target) / fee_base - PricingEngine.marketplace_fee_rate()
        return float(ad.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


    @staticmethod
    def calculate_selling_price(total_cost: float, target_margin: float = 0.15) -> dict:
        """
        Calculate Listing Price for Target Margin (考虑店铺折扣).

        公式推导 (考虑 5% 买家折扣):
          买家实付 = price × (1 - discount)
          eBay 费用 = 买家实付 × (FVF + AD)
          净收 = 买家实付 × (1 - FVF - AD) - 固定费
          利润 = 净收 - 成本 = price × margin  (margin 相对于标价的百分比)

          price × (1-d)(1-f) - F - C = price × M
          price = (C + F) / ((1-d)(1-f) - M)

        Returns:
            dict with selling_price and breakdown
        """
        if total_cost < 0:
            raise ValueError(f"Invalid total_cost: {total_cost}")
        if not (0 <= target_margin < 0.70):
            raise ValueError(f"Invalid target_margin: {target_margin} (must be 0~0.70)")
        
        cost = Decimal(str(total_cost))
        margin = Decimal(str(target_margin))
        
        total_rate = PricingEngine._variable_take() * (
            Decimal("1") + PricingEngine.LISTING_TAX_PAD
        )
        discount = PricingEngine.STORE_DISCOUNT_RATE
        # (1 - discount) × (1 - padded take) - margin
        denominator = (Decimal("1.0") - discount) * (Decimal("1.0") - total_rate) - margin
        
        if denominator <= Decimal("0.01"):
            raise ValueError(f"Margin {target_margin:.1%} + fees {float(total_rate):.1%} + discount {float(discount):.1%} leaves no revenue")
            
        price = (cost + PricingEngine.FIXED_FEE) / denominator
        selling_price = float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        # Publish / inventory 15% target still cannot slip under the 10% sale-price floor.
        try:
            policy_floor = PricingEngine.net_margin_floor_price(cost)
            if selling_price < policy_floor:
                selling_price = policy_floor
        except ValueError:
            pass

        return {
            "selling_price": selling_price,
            "total_cost": float(cost),
            "target_margin": float(margin),
            "ebay_fee_rate": float(total_rate),
            "store_discount": float(discount),
            "status": "OK"
        }
    
    @staticmethod
    def calculate_smart_price(total_cost: float, market_price: float,
                             min_margin: float = 0.10,
                             max_margin: float = 0.35) -> dict:
        """
        智能定价算法 (Market-Aware Dynamic Pricing)
        
        所有价格均为 eBay listing price (已包含 eBay 费用 + 店铺折扣)。
        
        考虑 5% 店铺折扣后的卖家净收 (含国际费与 10% 税垫):
          净收 = listing_price × discount_denom - $0.40
          净利率 = (净收 - 成本) / listing_price  (售价口径, 不是成本加价)
        """
        refused = {
            "final_price": None,
            "floor_price": None,
            "ceiling_price": None,
            "competitive_price": None,
            "market_price": None,
            "strategy": "REFUSED",
            "margin": None,
            "min_margin": min_margin,
            "max_margin": max_margin,
            "status": "INVALID_COST",
            "reason": "unknown_or_invalid_cost",
            "pause_ad": False,
        }
        cost = parse_money(total_cost)
        if cost is None:
            return refused

        try:
            market = (
                Decimal(str(market_price))
                if market_price and parse_money(market_price) is not None
                else Decimal("0")
            )
        except (InvalidOperation, ValueError, TypeError):
            market = Decimal("0")
        min_m = Decimal(str(min_margin))
        max_m = Decimal(str(max_margin))
        policy = PricingEngine.min_net_margin()

        discount_denom = PricingEngine._discount_denom()
        fixed_fee = PricingEngine.FIXED_FEE

        floor_result = PricingEngine.compute_net_margin_floor(cost, min_margin=min_m)
        if not floor_result["ok"]:
            refused["reason"] = floor_result["reason"]
            refused["status"] = "INVALID_FLOOR"
            return refused
        floor_lp = floor_result["price"]
        ceiling_lp = (cost * (1 + max_m) + fixed_fee) / discount_denom

        # 如果没有市场价格，使用传统定价 (15% 利润)
        if market <= 0:
            target_price = PricingEngine.calculate_selling_price(float(cost), 0.15)
            final_price = Decimal(str(target_price["selling_price"]))
            if final_price < floor_lp:
                final_price = floor_lp
            _profit, actual_margin, _reason = PricingEngine.net_profit_and_margin(
                final_price, cost
            )
            return {
                "final_price": float(final_price),
                "floor_price": float(floor_lp),
                "ceiling_price": float(ceiling_lp.quantize(_CENTS, rounding=ROUND_HALF_UP)),
                "competitive_price": None,
                "market_price": None,
                "strategy": "STANDARD",
                "margin": float(actual_margin.quantize(Decimal("0.001"))) if actual_margin is not None else 0.15,
                "min_margin": min_margin,
                "max_margin": max_margin,
                "status": "NO_MARKET_DATA",
                "pause_ad": False,
            }

        # 竞争性定价 (比市场低 5%)
        competitive_lp = market * Decimal("0.95")

        # 应用定价公式: max(Floor, min(Competitive, Ceiling))
        inner_min = min(competitive_lp, ceiling_lp)
        final_lp = max(floor_lp, inner_min)

        # 防亏损保护: 不低于售价口径净利底, 即使调用方传入更低的 min_margin
        min_safe = max(min_m, policy)
        anti_loss = PricingEngine.compute_net_margin_floor(cost, min_margin=min_safe)
        if not anti_loss["ok"]:
            refused["reason"] = anti_loss["reason"]
            refused["status"] = "INVALID_FLOOR"
            return refused
        anti_loss_floor = anti_loss["price"]

        if final_lp < anti_loss_floor:
            final_lp = anti_loss_floor
            strategy = "ANTI_LOSS"
            status = "DISCOUNT_PROTECTED"
        elif final_lp <= floor_lp:
            strategy = "FLOOR_PRICE"
            status = "TIGHT_MARKET"
        elif final_lp >= ceiling_lp:
            strategy = "CEILING_PRICE"
            status = "HIGH_MARGIN"
        else:
            strategy = "COMPETITIVE"
            status = "OPTIMAL"

        if not isinstance(final_lp, Decimal):
            final_lp = Decimal(str(final_lp))
        if final_lp == anti_loss_floor or final_lp == floor_lp:
            priced = final_lp
        else:
            priced = final_lp.quantize(_CENTS, rounding=ROUND_HALF_UP)
            if priced < floor_lp:
                priced = floor_lp

        _profit, actual_margin, _reason = PricingEngine.net_profit_and_margin(
            priced, cost
        )

        return {
            "final_price": float(priced),
            "floor_price": float(floor_lp),
            "ceiling_price": float(ceiling_lp.quantize(_CENTS, rounding=ROUND_HALF_UP)),
            "competitive_price": float(competitive_lp.quantize(_CENTS, rounding=ROUND_HALF_UP)),
            "market_price": float(market),
            "strategy": strategy,
            "margin": float(actual_margin.quantize(Decimal("0.001"))) if actual_margin is not None else Decimal("0"),
            "min_margin": min_margin,
            "max_margin": max_margin,
            "status": status,
            "pause_ad": False,
        }

    @staticmethod
    def determine_final_price(safe_price: float, min_price: float, market_price: float | None) -> dict:
        """
        Decision Matrix:
        - If Market > Safe: Sell at Market - 0.01 (Maximize Profit)
        - If Market < Safe but > Min: Sell at Safe (Protect Target 15%)
          *Correction per user req*: "If Market < Safe but > Min, Sell at Safe" -> Actually usually we'd sell at Market to be competitive, 
          BUT user instruction said: "If Market < Safe but > Min, take Safe Price". (Adhering to strict instruction).
        - If Market < Min: "Not Competitive"
        """
        if market_price is None or market_price <= 0:
             return {"price": safe_price, "strategy": "SAFE_DEFAULT", "status": "LIST"}

        if market_price > safe_price:
            return {"price": market_price - 0.01, "strategy": "MARKET_MAXIMIZE", "status": "LIST"}
        
        if market_price > min_price:
            # Market is tight but profitable enough to list at our Safe price?
            # User rule: "Take Safe_Price". (Likely willing to wait or have better listing quality)
            return {"price": safe_price, "strategy": "PROTECT_MARGIN", "status": "LIST"}
            
        # Market < Min Price
        return {"price": min_price, "strategy": "UNCOMPETITIVE", "status": "SKIP"}
