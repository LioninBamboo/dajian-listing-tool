from decimal import Decimal, ROUND_HALF_UP

class PricingEngine:
    """
    Dajian -> eBay Pricing Calculator (AquaVerve Financial Model)
    """

    # --- Constants ---
    RETURN_INSURANCE_RATE = Decimal("0.02")      # 2% 退货保障
    LOGISTICS_INSURANCE_EXPRESS = Decimal("0.032") # 3.2% 快递物流保障
    LOGISTICS_INSURANCE_FREIGHT = Decimal("0.05")  # 5% 卡车/大件物流保障
    PAYMENT_FEE_RATE = Decimal("0.0083")         # 0.83% 支付手续费

    # eBay Costs
    EBAY_FEE_RATE = Decimal("0.1325") # 13.25%
    AD_RATE = Decimal("0.05")         # 5.00%
    FIXED_FEE = Decimal("0.30")       # $0.30

    @staticmethod
    def calculate_dajian_cost(product_price: float, shipping_cost: float, is_oversize: bool = False) -> dict:
        """
        Calculate Total Acquisition Cost from Dajian.
        """
        base_cost = Decimal(str(product_price)) + Decimal(str(shipping_cost))
        
        # 1. Return Insurance
        return_ins = base_cost * PricingEngine.RETURN_INSURANCE_RATE
        
        # 2. Logistics Insurance
        logistics_rate = PricingEngine.LOGISTICS_INSURANCE_FREIGHT if is_oversize else PricingEngine.LOGISTICS_INSURANCE_EXPRESS
        logistics_ins = base_cost * logistics_rate
        
        # 3. Payment Fee (Applied to Base + Insurance)
        subtotal = base_cost + return_ins + logistics_ins
        payment_fee = subtotal * PricingEngine.PAYMENT_FEE_RATE
        
        total_dajian_cost = subtotal + payment_fee
        
        return {
            "base_cost": float(base_cost),
            "return_insurance": float(return_ins),
            "logistics_insurance": float(logistics_ins),
            "payment_fee": float(payment_fee),
            "total_dajian_cost": float(total_dajian_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        }

    @staticmethod
    def calculate_selling_price(total_cost: float, target_margin: float = 0.15) -> float:
        """
        Calculate Listing Price for Target Margin.
        Formula: Price = (Cost + 0.30) / (1 - Fees - Margin)
        """
        cost = Decimal(str(total_cost))
        margin = Decimal(str(target_margin))
        
        total_rate = PricingEngine.EBAY_FEE_RATE + PricingEngine.AD_RATE
        denominator = Decimal("1.0") - total_rate - margin
        
        if denominator <= 0:
            return 9999.99 # Impossible margin
            
        price = (cost + PricingEngine.FIXED_FEE) / denominator
        return float(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

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
