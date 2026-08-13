from decimal import Decimal, ROUND_HALF_UP

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

    # eBay Costs
    EBAY_FEE_RATE = Decimal("0.1325") # 13.25%
    AD_RATE = Decimal("0.05")         # 5.00%
    FIXED_FEE = Decimal("0.30")       # $0.30

    # Store Discount (长期店铺折扣)
    STORE_DISCOUNT_RATE = Decimal("0.05")  # 5% 买家折扣

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

    # ════════════════════════════════════════════════════════════════════
    # 死线 / 安全底 / 守门员 — 所有改价入口必须遵守的统一防线
    # ════════════════════════════════════════════════════════════════════
    #
    # 公式推导 (考虑 5% 店铺折扣 + 13.25% eBay FVF + 5% 广告 + $0.30 固定费):
    #   买家实付 = listing_price × (1 - discount)
    #   卖家净收 = 买家实付 × (1 - fvf - ad) - fixed_fee
    #            = listing_price × discount_denom - fixed_fee
    #   利润    = 净收 - cost
    #
    # 死线 (margin=0): 净收 = cost  →  listing_price = (cost + fixed) / discount_denom
    # 安全底 (margin=s): 净收 = cost × (1+s)  →  listing_price = (cost×(1+s) + fixed) / discount_denom

    @staticmethod
    def _discount_denom(ad_rate: float = None) -> Decimal:
        """(1 - 折扣) × (1 - eBay FVF - ad_rate).

        ad_rate=None  → 用 PricingEngine.AD_RATE (5%, 默认假设广告开着)
        ad_rate=0     → 假设广告关掉, 死线下降 (扩大让价空间)
        ad_rate=0.04  → 假设降低竞价率
        """
        ad = PricingEngine.AD_RATE if ad_rate is None else Decimal(str(ad_rate))
        if ad < 0 or ad >= Decimal("1") - PricingEngine.EBAY_FEE_RATE:
            raise ValueError(f"ad_rate must be in [0, {1 - float(PricingEngine.EBAY_FEE_RATE)}), got {ad_rate}")
        return (Decimal("1") - PricingEngine.STORE_DISCOUNT_RATE) * (
            Decimal("1") - PricingEngine.EBAY_FEE_RATE - ad
        )

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
        # 净收 = price × discount × (1 - fvf - ad) - fixed = cost × (1 + safety)
        # → (1 - fvf - ad) = (cost(1+safety) + fixed) / (price × discount)
        # → ad = 1 - fvf - (cost(1+safety) + fixed) / (price × discount)
        if p * discount <= 0:
            return -1.0
        max_combined = (cost * (Decimal("1") + safety) + PricingEngine.FIXED_FEE) / (p * discount)
        ad = Decimal("1") - PricingEngine.EBAY_FEE_RATE - max_combined
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
        
        total_rate = PricingEngine.EBAY_FEE_RATE + PricingEngine.AD_RATE
        discount = PricingEngine.STORE_DISCOUNT_RATE
        # (1 - discount) × (1 - fees) - margin
        denominator = (Decimal("1.0") - discount) * (Decimal("1.0") - total_rate) - margin
        
        if denominator <= Decimal("0.01"):
            raise ValueError(f"Margin {target_margin:.1%} + fees {float(total_rate):.1%} + discount {float(discount):.1%} leaves no revenue")
            
        price = (cost + PricingEngine.FIXED_FEE) / denominator
        selling_price = float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        
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
        
        考虑 5% 店铺折扣后的卖家净收:
          净收 = listing_price × (1 - 折扣) × (1 - eBay费率 - 广告费率) - 固定费
               = listing_price × 0.95 × 0.8175 - $0.30
        
        公式 (所有边界均基于折扣后利润):
          discount_denom = 0.95 × 0.8175  (= 0.776625)
          floor_lp   = (cost × (1 + min_margin) + $0.30) / discount_denom
          ceiling_lp = (cost × (1 + max_margin) + $0.30) / discount_denom
          competitive_lp = market × 0.95
          final_lp = max(floor_lp, min(competitive_lp, ceiling_lp))
        """
        cost = Decimal(str(total_cost))
        market = Decimal(str(market_price)) if market_price and market_price > 0 else Decimal("0")
        min_m = Decimal(str(min_margin))
        max_m = Decimal(str(max_margin))
        
        # eBay 费用参数 (考虑店铺折扣)
        total_fee_rate = PricingEngine.EBAY_FEE_RATE + PricingEngine.AD_RATE  # 0.1825
        denom = Decimal("1") - total_fee_rate  # 0.8175
        discount_rate = Decimal("1") - PricingEngine.STORE_DISCOUNT_RATE  # 0.95
        discount_denom = discount_rate * denom  # 0.776625
        fixed_fee = PricingEngine.FIXED_FEE  # $0.30
        
        # 计算 listing price 边界 (基于折扣后净收)
        floor_lp = (cost * (1 + min_m) + fixed_fee) / discount_denom
        ceiling_lp = (cost * (1 + max_m) + fixed_fee) / discount_denom
        
        # 如果没有市场价格，使用传统定价 (15% 利润)
        if market <= 0:
            target_price = PricingEngine.calculate_selling_price(float(cost), 0.15)
            return {
                "final_price": target_price["selling_price"],
                "floor_price": float(floor_lp.quantize(Decimal("0.01"))),
                "ceiling_price": float(ceiling_lp.quantize(Decimal("0.01"))),
                "competitive_price": None,
                "market_price": None,
                "strategy": "STANDARD",
                "margin": 0.15,
                "status": "NO_MARKET_DATA"
            }
        
        # 竞争性定价 (比市场低 5%)
        competitive_lp = market * Decimal("0.95")
        
        # 应用定价公式: max(Floor, min(Competitive, Ceiling))
        inner_min = min(competitive_lp, ceiling_lp)
        final_lp = max(floor_lp, inner_min)
        
        # 防亏损保护: 考虑折扣后仍保证 ≥5% 利润
        min_safe_margin = Decimal("1.05")
        anti_loss_floor = (cost * min_safe_margin + fixed_fee) / discount_denom
        
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
        
        # 计算实际利润率 (基于折扣后卖家净收)
        net_revenue = final_lp * discount_denom - fixed_fee
        actual_margin = (net_revenue - cost) / net_revenue if net_revenue > 0 else Decimal("0")
        
        return {
            "final_price": float(final_lp.quantize(Decimal("0.01"))),
            "floor_price": float(floor_lp.quantize(Decimal("0.01"))),
            "ceiling_price": float(ceiling_lp.quantize(Decimal("0.01"))),
            "competitive_price": float(competitive_lp.quantize(Decimal("0.01"))),
            "market_price": float(market),
            "strategy": strategy,
            "margin": float(actual_margin.quantize(Decimal("0.001"))),
            "min_margin": min_margin,
            "max_margin": max_margin,
            "status": status
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
