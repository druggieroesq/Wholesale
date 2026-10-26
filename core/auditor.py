import time
from typing import Dict, Any, Tuple

class SafetyAuditor:
    """
    Audits Keepa product data to detect IP risk, Amazon 1P dominance,
    rapid seller drops (brand enforcement lawsuits), and sales velocity.
    """
    KEEPA_CSV_AMAZON_1P_INDEX = 0
    KEEPA_CSV_NEW_3P_FBA_INDEX = 10
    KEEPA_CSV_RATING_INDEX = 16

    @classmethod
    def get_monthly_sales_drops(cls, keepa_prod: Dict[str, Any]) -> int:
        """
        Extracts 30-day estimated sales drops (BSR drops) from Keepa stats.
        Falls back to 90-day average divided by 3, or returns 0 if absent.
        """
        stats = keepa_prod.get("stats", {})
        monthly_drops = stats.get("salesRankDrops30", 0)
        if monthly_drops and monthly_drops > 0:
            return int(monthly_drops)

        drops_90 = stats.get("salesRankDrops90", 0)
        if drops_90 and drops_90 > 0:
            return max(1, int(drops_90 / 3))

        return 0

    @classmethod
    def audit_product(cls, keepa_prod: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Evaluates risk factors.
        Returns (True, 'CLEARED') if safe, or (False, reason) if unsafe.
        """
        stats = keepa_prod.get("stats", {})

        # 1. Amazon 1P In-Stock / Dominance Check
        # If Amazon is on the listing >= 70% of the last 90 days, skip.
        amz_share_90 = stats.get("buyBoxAmazon90", 0)
        if amz_share_90 and amz_share_90 >= 70:
            return False, f"Amazon 1P dominates Buy Box ({amz_share_90}% share in 90d)"

        # 2. Seller Count Drop / IP Lawsuit Sweep Audit
        # Check if FBA sellers plummeted sharply (classic IP takedown indicator)
        live_sellers = keepa_prod.get("liveSellers", [])
        current_fba = stats.get("current_fba_sellers", 0)

        if len(live_sellers) >= 2:
            peak_sellers = max(live_sellers)
            if peak_sellers >= 5 and current_fba <= 1:
                return False, f"Suspected Brand IP Sweep: Sellers collapsed from {peak_sellers} to {current_fba}"

        # 3. Minimum Sales Velocity Check
        monthly_drops = cls.get_monthly_sales_drops(keepa_prod)
        if monthly_drops < 5:
            return False, f"Insufficient sales velocity ({monthly_drops} drops in 30d)"

        # 4. Variation / Review Manipulation Check
        total_reviews = stats.get("reviewCount", 0)
        rating = stats.get("avgRating", 0)
        # Ratings in Keepa are often stored as integers (e.g., 42 = 4.2 stars)
        if rating > 5:
            rating = rating / 10.0

        if total_reviews >= 10 and rating > 0 and rating < 3.2:
            return False, f"Poor customer sentiment: {rating} stars on {total_reviews} reviews"

        return True, "CLEARED"
