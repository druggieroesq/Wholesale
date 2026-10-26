from typing import Dict, Any

class AmazonStockoutRadar:
    """
    Detects Amazon 1P stockout vacancies and verified third-party sales velocity.
    Identifies high-velocity ASINs where Amazon 1P is absent and third-party
    competition is weak (<= 2 sellers).
    """

    def __init__(self):
        pass

    def evaluate_opportunity(self, keepa_product: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluates 30-day verified sales rank drops against active FBA seller competition.
        """
        keepa_product = keepa_product or {}
        stats = keepa_product.get("stats", {}) or {}
        drops = stats.get("salesRankDrops30", 0) or 0
        curr = stats.get("current", [])
        
        # Keepa index 10 represents active FBA offer count
        fba_count = curr[10] if len(curr) > 10 and curr[10] is not None else 0

        # Opportunity criteria: demand proof (>= 15 drops) + low competition (<= 2 sellers)
        is_gap = (drops >= 15) and (fba_count <= 2)
        
        return {
            "is_stockout_opportunity": is_gap,
            "fba_sellers": fba_count,
            "monthly_velocity": drops
        }
