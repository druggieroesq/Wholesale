from typing import Dict, Any

class IncentiveOptimizer:
    """
    Evaluates supplier incentives, Free Freight Allowances (FFA),
    and volume discount tiers against purchase order totals.
    """

    def __init__(self):
        pass

    def calculate_order_perks(
        self, 
        distributor_meta: Dict[str, Any], 
        gross_spend: float
    ) -> Dict[str, Any]:
        """
        Determines if an order qualifies for Free Freight Allowance (FFA)
        or volume discount brackets.
        """
        distributor_meta = distributor_meta or {}
        ffa_threshold = float(distributor_meta.get("free_freight_minimum", 2000.0))
        qualifies_ffa = gross_spend >= ffa_threshold

        discount_pct = 0.0
        if gross_spend >= 5000.0:
            discount_pct = 0.03
        elif gross_spend >= 2500.0:
            discount_pct = 0.015

        early_pay = float(distributor_meta.get("payment_terms", {}).get("early_pay_discount_pct", 0.0))

        return {
            "gross_spend": round(gross_spend, 2),
            "free_freight_unlocked": qualifies_ffa,
            "volume_discount_pct": discount_pct,
            "early_pay_discount_pct": early_pay,
            "total_rebate_pct": round(discount_pct + early_pay, 4)
        }
