from typing import Dict, Any, Optional

class InboundFreightEstimator:
    """
    Calculates accurate inbound LTL/SPD freight and Amazon Inbound Placement Service fees
    based on product weight and size tiers.
    """
    BASE_RATE_PER_LB = 0.35  # Standard SPD (Small Parcel Delivery) average to East Coast FCs
    MINIMUM_SHIP_FEE_PER_UNIT = 0.20

    @classmethod
    def estimate_inbound_cost(cls, weight_lbs: float, is_liquid_or_glass: bool = False) -> float:
        """
        Estimates total inbound landing cost per unit (carrier partner rate + prep allocation).
        """
        weight = max(0.1, weight_lbs)
        
        # Tiered weight scaling
        if weight <= 0.5:
            freight = cls.MINIMUM_SHIP_FEE_PER_UNIT
        elif weight <= 1.0:
            freight = 0.30
        elif weight <= 2.0:
            freight = 0.55
        elif weight <= 5.0:
            freight = round(weight * cls.BASE_RATE_PER_LB, 2)
        else:
            # Bulky items
            freight = round(weight * 0.42, 2)

        # Glass or liquid packaging requires bubble bags or tape prep allocation
        if is_liquid_or_glass:
            freight += 0.15

        return round(freight, 2)
