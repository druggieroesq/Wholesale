import math
from typing import Dict, Any

class InventoryPlanner:
    """
    Calculates operational inventory allocation, order ceilings, and Days of Inventory (DOI)
    to target a 30 to 45 day inventory turn cycle across Amazon FBA and Walmart WFS.
    """

    def __init__(self, target_days: int = 35, max_days: int = 45):
        self.target_days = target_days
        self.max_days = max_days

    def calculate_order_ceiling(
        self,
        keepa_product: Dict[str, Any],
        fba_seller_count: int = 1,
        case_pack: int = 6
    ) -> Dict[str, int]:
        stats = keepa_product.get("stats", {}) or {}
        monthly_drops = stats.get("salesRankDrops30", 0) or 0
        
        # 1.25x multiplier on verified drops to estimate real monthly unit volume
        estimated_monthly_velocity = max(int(monthly_drops * 1.25), 6)
        active_sellers = max(int(fba_seller_count), 1)
        expected_monthly_share = max(1, math.ceil(estimated_monthly_velocity / active_sellers))
        
        target_units = math.ceil((expected_monthly_share / 30.0) * self.target_days)
        max_units = math.ceil((expected_monthly_share / 30.0) * self.max_days)
        
        def round_case(units: int, pack: int) -> int:
            if pack <= 1:
                return units
            return max(pack, math.ceil(units / pack) * pack)

        amz_recommended = round_case(target_units, case_pack)
        amz_max = round_case(max_units, case_pack)
        
        # Walmart allocation: conservative 30% to 35% of Amazon volume
        wm_recommended = round_case(max(case_pack, math.ceil(amz_recommended * 0.30)), case_pack)
        wm_max = round_case(max(case_pack, math.ceil(amz_max * 0.35)), case_pack)

        return {
            "amz_recommended": amz_recommended,
            "amz_max_ceiling": amz_max,
            "wm_recommended": wm_recommended,
            "wm_max_ceiling": wm_max
        }
