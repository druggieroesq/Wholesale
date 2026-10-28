import os
import logging
from typing import Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FeeCalculator")

class FeeCalculator:
    """
    Amazon & Parkland Self-Prep fee calculator.
    Enforces strict 50% ROI floor and accurate net margin modeling.
    """
    REFERRAL_FEE_PCT = 0.15  # Amazon Standard Marketplace Commission
    PARKLAND_PREP_FEE = 0.35  # Local Self-Prep per unit
    ESTIMATED_INBOUND_LBS = 0.65  # Inbound shipping buffer per unit

    @classmethod
    def estimate_fba_pick_pack(cls, weight_lbs: float) -> float:
        """
        Amazon FBA size tier fulfillment schedule.
        """
        if weight_lbs <= 1.0:
            return 3.86
        elif weight_lbs <= 2.0:
            return 5.40
        elif weight_lbs <= 3.0:
            return 6.10
        else:
            return round(6.10 + (weight_lbs - 3.0) * 0.45, 2)

    @classmethod
    def calculate_deal_economics(
        cls, 
        wholesale_cost: float, 
        amazon_price: float, 
        weight_lbs: float = 1.5,
        prep_override: float = None
    ) -> Dict[str, Any]:
        """
        Evaluates full deal math against Amazon fees and Parkland prep costs.
        """
        prep_cost = prep_override if prep_override is not None else cls.PARKLAND_PREP_FEE
        
        # 1. Amazon Marketplace Overhead
        referral_fee = round(amazon_price * cls.REFERRAL_FEE_PCT, 2)
        fba_fee = round(cls.estimate_fba_pick_pack(weight_lbs), 2)
        total_amazon_fees = round(referral_fee + fba_fee, 2)

        # 2. Total Inbound Landed Cost
        landed_unit_cost = round(wholesale_cost + prep_cost + cls.ESTIMATED_INBOUND_LBS, 2)

        # 3. Net Margin & Return on Investment
        net_profit = round(amazon_price - total_amazon_fees - landed_unit_cost, 2)
        roi_pct = round((net_profit / landed_unit_cost) * 100.0, 1) if landed_unit_cost > 0 else 0.0

        passes_roi_floor = roi_pct >= 50.0

        return {
            "wholesale_cost": wholesale_cost,
            "amazon_price": amazon_price,
            "referral_fee": referral_fee,
            "fba_fee": fba_fee,
            "total_amazon_fees": total_amazon_fees,
            "parkland_prep_fee": prep_cost,
            "landed_unit_cost": landed_unit_cost,
            "net_profit": net_profit,
            "roi_pct": roi_pct,
            "passes_roi_floor": passes_roi_floor
        }


if __name__ == "__main__":
    calc = FeeCalculator()
    sample = calc.calculate_deal_economics(wholesale_cost=11.50, amazon_price=36.99, weight_lbs=1.5)
    print("Fee & ROI Calculation:")
    for k, v in sample.items():
        print(f"  {k}: {v}")
