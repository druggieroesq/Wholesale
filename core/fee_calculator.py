from typing import Dict, Any, Optional

class DynamicFeeCalculator:
    """
    Calculates Amazon FBA referral and fulfillment fees dynamically.
    Prioritizes real-time fee fields from Keepa; falls back to a 
    configurable tier schedule if API values are absent.
    """

    # Default fallback schedule (easily updated via config or JSON)
    FALLBACK_FBA_TIERS = [
        {"max_weight_lbs": 0.25, "fee": 3.22},
        {"max_weight_lbs": 0.50, "fee": 3.40},
        {"max_weight_lbs": 0.75, "fee": 3.58},
        {"max_weight_lbs": 1.00, "fee": 3.77},
        {"max_weight_lbs": 1.50, "fee": 4.42},
        {"max_weight_lbs": 2.00, "fee": 4.95},
        {"max_weight_lbs": 3.00, "fee": 5.45},
    ]
    PER_LB_OVER_3_LBS = 0.38

    @classmethod
    def get_fba_fulfillment_fee(cls, keepa_prod: Optional[Dict[str, Any]] = None, fallback_weight_lbs: float = 1.0) -> float:
        """
        1. Checks Keepa's live 'fbaFees' field (auto-synced with Amazon).
        2. Falls back to weight calculation if unavailable.
        """
        if keepa_prod:
            live_fee_cents = keepa_prod.get("fbaFees")
            if live_fee_cents and isinstance(live_fee_cents, (int, float)) and live_fee_cents > 0:
                return round(live_fee_cents / 100.0, 2)

            # Check if Keepa recorded package weight in grams
            package_weight_grams = keepa_prod.get("packageWeight")
            if package_weight_grams and package_weight_grams > 0:
                fallback_weight_lbs = package_weight_grams / 453.592

        # Fallback tier lookup
        for tier in cls.FALLBACK_FBA_TIERS:
            if fallback_weight_lbs <= tier["max_weight_lbs"]:
                return tier["fee"]

        # Over max tier
        base_fee = cls.FALLBACK_FBA_TIERS[-1]["fee"]
        extra_weight = fallback_weight_lbs - cls.FALLBACK_FBA_TIERS[-1]["max_weight_lbs"]
        return round(base_fee + (extra_weight * cls.PER_LB_OVER_3_LBS), 2)

    @classmethod
    def get_referral_fee_rate(cls, buy_box_price: float, keepa_prod: Optional[Dict[str, Any]] = None, is_beauty_or_health: bool = True) -> float:
        """
        Extracts live category referral rate or applies category-specific rules
        (such as Beauty/Health 8% under $10 vs 15% standard).
        """
        if keepa_prod:
            api_referral_pct = keepa_prod.get("referralFeePercent")
            if api_referral_pct and isinstance(api_referral_pct, (int, float)) and api_referral_pct > 0:
                return api_referral_pct / 100.0

        # Category rule: Beauty/Personal Care sub-$10 incentive
        if is_beauty_or_health and buy_box_price <= 10.0:
            return 0.08

        return 0.15

    @classmethod
    def calculate_net_economics(
        cls, 
        buy_box: float, 
        unit_cost: float, 
        keepa_prod: Optional[Dict[str, Any]] = None, 
        weight_lbs: float = 1.0,
        inbound_shipping_rate_per_lb: float = 0.30
    ) -> Dict[str, Any]:
        if buy_box <= 0:
            return {"net_profit": 0.0, "roi": 0.0, "margin": 0.0, "fba_fee": 0.0, "referral_fee": 0.0}

        # 1. Determine dynamic fulfillment fee
        fba_fee = cls.get_fba_fulfillment_fee(keepa_prod=keepa_prod, fallback_weight_lbs=weight_lbs)

        # 2. Determine dynamic referral fee
        referral_rate = cls.get_referral_fee_rate(buy_box, keepa_prod=keepa_prod)
        referral_fee = round(buy_box * referral_rate, 2)

        # 3. Estimated inbound SPD shipping cost
        inbound_cost = round(weight_lbs * inbound_shipping_rate_per_lb, 2)

        # 4. Total deductions
        total_fees = referral_fee + fba_fee + inbound_cost
        net_profit = round(buy_box - (unit_cost + total_fees), 2)
        roi = round((net_profit / unit_cost) * 100, 1) if unit_cost > 0 else 0.0
        margin = round((net_profit / buy_box) * 100, 1)

        return {
            "net_profit": net_profit,
            "roi": roi,
            "margin": margin,
            "fba_fee": fba_fee,
            "referral_fee": referral_fee,
            "inbound_shipping": inbound_cost,
            "is_dynamic_api_fee": bool(keepa_prod and keepa_prod.get("fbaFees"))
        }
