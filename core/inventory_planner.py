from typing import Dict, Any
import math

class InventoryPlanner:
    """
    Calculates order ceilings and Days of Inventory (DOI)
    to target a 30 to 45 day inventory turn cycle.
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
        
        # Estimate total monthly units sold (~1.25x drops as a conservative multiplier)
        estimated_monthly_velocity = max(int(monthly_drops * 1.25), 6)
        
        # Share of Buy Box velocity based on competitive FBA seller count
        active_sellers = max(int(fba_seller_count), 1)
        expected_monthly_share = max(1, math.ceil(estimated_monthly_velocity / active_sellers))
        
        # Target units for a 35-day turn
        target_units = math.ceil((expected_monthly_share / 30.0) * self.target_days)
        max_units = math.ceil((expected_monthly_share / 30.0) * self.max_days)
        
        # Round up to nearest case pack
        def round_case(units: int, pack: int) -> int:
            if pack <= 1:
                return units
            return max(pack, math.ceil(units / pack) * pack)

        amz_recommended = round_case(target_units, case_pack)
        amz_max = round_case(max_units, case_pack)
        
        # Walmart allocation: conservative 20% to 35% of Amazon volume
        wm_recommended = round_case(max(case_pack, math.ceil(amz_recommended * 0.30)), case_pack)
        wm_max = round_case(max(case_pack, math.ceil(amz_max * 0.35)), case_pack)

        return {
            "amz_recommended": amz_recommended,
            "amz_max_ceiling": amz_max,
            "wm_recommended": wm_recommended,
            "wm_max_ceiling": wm_max
        }
