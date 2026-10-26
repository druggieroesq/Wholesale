from typing import Dict, Any, Tuple

class SafetyAuditor:
    """
    Audits Keepa product listings for zero-tolerance risk factors:
    - Amazon 1P dominance (> 15% Buy Box share)
    - Hazmat, dangerous goods, and aerosol flags
    - Meltable / heat-sensitive seasonal goods
    - IP complaint / private label brand risk
    """

    KNOWN_IP_AGGRESSIVE_BRANDS = {
        "apple", "nike", "disney", "lego", "sony", "bose", 
        "otterbox", "hydro flask", "yeti", "popsockets", "anker"
    }

    MELTABLE_KEYWORDS = [
        "chocolate", "gummy", "gummies", "wax", "candle", 
        "melt", "softgel", "marshmallow"
    ]

    def audit_product(
        self,
        keepa_product: Dict[str, Any],
        row_data: Dict[str, Any] = None,
        max_amz_share: float = 0.15
    ) -> Tuple[bool, str]:
        row_data = row_data or {}
        stats = keepa_product.get("stats", {}) or {}
        title = (keepa_product.get("title") or row_data.get("title", "")).lower()
        brand = (keepa_product.get("brand") or row_data.get("brand", "")).lower().strip()

        # 1. IP Aggressive Brand Check
        if brand in self.KNOWN_IP_AGGRESSIVE_BRANDS:
            return False, f"IP Risk: Brand '{brand}' strictly enforces Brand Registry takedowns."

        # 2. Amazon 1P Dominance Check
        amz_oos_90 = stats.get("outOfStockPercentage90", {})
        amz_oos_rate = 0
        if isinstance(amz_oos_90, list) and len(amz_oos_90) > 0:
            amz_oos_rate = amz_oos_90[0]
        elif isinstance(amz_oos_90, (int, float)):
            amz_oos_rate = amz_oos_90

        if amz_oos_rate > 0 and amz_oos_rate < (100 - (max_amz_share * 100)):
            return False, f"Amazon 1P Dominance: Amazon owns Buy Box > {max_amz_share * 100}% of past 90 days."

        # 3. Hazmat & Dangerous Goods Check
        if keepa_product.get("isDangerousGoods", False) or keepa_product.get("isHazmat", False):
            return False, "Hazmat: Flammable or hazardous materials certification required."

        # 4. Meltable Check
        if any(w in title for w in self.MELTABLE_KEYWORDS):
            return False, "Meltable: Temperature-sensitive product prohibited during warm seasons."

        # 5. Adult Category Check
        if keepa_product.get("isAdultProduct", False):
            return False, "Restricted: Adult category product."

        return True, "PASSED_ALL_SAFETY_CHECKS"
