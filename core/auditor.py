from typing import Dict, Any, Optional
from config import MAX_AMAZON_1P_SHARE

class CatalogAuditor:
    @staticmethod
    def audit_sku_safety(prod: Dict[str, Any], wholesale_cost: float) -> Optional[Dict[str, Any]]:
        stats = prod.get("stats", {})
        current = stats.get("current", [])

        amz_oos_90 = stats.get("outOfStockPercentageAmazon90", 100)
        amz_in_stock_share = (100 - amz_oos_90) / 100.0

        if amz_in_stock_share > MAX_AMAZON_1P_SHARE:
            return None

        sellers = current[11] if len(current) > 11 else 0
        if sellers < 2 or sellers > 12:
            return None

        monthly_drops = stats.get("salesRankDrops30", 0)
        if monthly_drops < 5:
            return None

        buy_box_cents = current[18] if len(current) > 18 else 0
        buy_box = (buy_box_cents / 100.0) if buy_box_cents > 0 else 0.0

        return {
            "asin": prod.get("asin"),
            "buy_box": buy_box,
            "monthly_drops": monthly_drops,
            "sellers_count": sellers
        }
