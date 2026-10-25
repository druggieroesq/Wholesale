import os
from typing import Dict, Any, Optional

class CatalogAuditor:
    """
    Evaluates Amazon ASIN safety, sales velocity, competition health,
    and brand IP complaint risks using Keepa metrics.
    """
    def __init__(
        self,
        max_amazon_1p_share: float = 15.0,
        min_monthly_drops: int = 15,
        min_fba_sellers: int = 2,
        max_fba_sellers: int = 20,
        max_seller_drop_pct: float = 70.0
    ):
        self.max_amazon_1p_share = max_amazon_1p_share
        self.min_monthly_drops = min_monthly_drops
        self.min_fba_sellers = min_fba_sellers
        self.max_fba_sellers = max_fba_sellers
        self.max_seller_drop_pct = max_seller_drop_pct

    def check_ip_complaint_risk(self, keepa_prod: Dict[str, Any]) -> bool:
        """
        Detects aggressive Brand IP enforcement.
        Flags listings where seller count collapsed abruptly (cease & desist wave)
        or where a single seller monopolizes the listing (Private Label risk).
        Returns True if risky (should be rejected), False if clean.
        """
        stats = keepa_prod.get("stats", {})
        if not stats:
            return False

        # 1. Monopolized / Private Label Check: Only 1 seller on listing
        current_sellers = stats.get("current", [])
        live_new_sellers = 1
        if len(current_sellers) > 11 and current_sellers[11] is not None:
            live_new_sellers = max(1, int(current_sellers[11]))

        # 90-day average seller count
        avg_sellers_90 = stats.get("avg90", [])
        avg_90 = 1.0
        if len(avg_sellers_90) > 11 and avg_sellers_90[11] is not None and avg_sellers_90[11] > 0:
            avg_90 = float(avg_sellers_90[11])

        # If historically or currently only 1 seller, high risk of brand ownership / private label
        if avg_90 <= 1.0 and live_new_sellers <= 1:
            return True

        # 2. Sudden Seller Crash Check: >70% seller drop indicates brand legal enforcement
        if avg_90 >= 4.0 and live_new_sellers <= 1:
            drop_pct = ((avg_90 - live_new_sellers) / avg_90) * 100.0
            if drop_pct >= self.max_seller_drop_pct:
                return True

        # 3. CSV historical seller array analysis if present
        csv_data = keepa_prod.get("csv", [])
        # Index 11 in Keepa CSV is COUNT_NEW (Seller Count timeline)
        if len(csv_data) > 11 and isinstance(csv_data[11], list) and len(csv_data[11]) >= 4:
            history = csv_data[11]
            # Inspect the last recorded values
            recent_counts = [history[i] for i in range(len(history)-1, -1, -2) if history[i] is not None][:4]
            if len(recent_counts) >= 2:
                peak = max(recent_counts)
                trough = min(recent_counts)
                if peak >= 5 and trough <= 1:
                    return True

        return False

    def audit_sku_safety(self, keepa_prod: Dict[str, Any], wholesale_cost: float) -> Optional[Dict[str, Any]]:
        """
        Audits candidate ASIN against all Amazon wholesale constraints:
        1. Amazon 1P presence (<15% in stock)
        2. Sales velocity (monthly rank drops >= 15)
        3. Healthy competition (no penny-dropping races or monopolization)
        4. IP / brand enforcement detection
        """
        if not keepa_prod:
            return None

        stats = keepa_prod.get("stats", {})
        if not stats:
            return None

        # Filter 1: Amazon 1P In-Stock Percentage
        # outOfStockPercentageAmazon90: 100 = Amazon was 100% OOS (Good)
        # We require Amazon to be in stock <= max_amazon_1p_share (e.g., OOS >= 85%)
        amazon_oos_rate = stats.get("outOfStockPercentageAmazon90", 100)
        amazon_in_stock_rate = 100 - amazon_oos_rate
        if amazon_in_stock_rate > self.max_amazon_1p_share:
            return None

        # Filter 2: Sales Velocity (30-day sales rank drops)
        monthly_drops = stats.get("salesRankDrops30", 0)
        if monthly_drops < self.min_monthly_drops:
            return None

        # Filter 3: Current Buy Box Price & Seller Count
        current = stats.get("current", [])
        if len(current) < 19:
            return None

        # Index 18 = Buy Box price in cents (index 1 = Amazon, index 2 = New Marketplace)
        buy_box_cents = current[18] if current[18] and current[18] > 0 else current[1]
        if not buy_box_cents or buy_box_cents <= 0:
            # Fall back to New Offer price
            buy_box_cents = current[2] if len(current) > 2 and current[2] and current[2] > 0 else 0

        if buy_box_cents <= 0:
            return None

        buy_box_price = round(buy_box_cents / 100.0, 2)

        # Index 11 = Total New Offer Count
        live_sellers = current[11] if len(current) > 11 and current[11] is not None else 1
        if live_sellers < self.min_fba_sellers or live_sellers > self.max_fba_sellers:
            return None

        # Filter 4: IP Complaint & Brand Enforcement Analysis
        if self.check_ip_complaint_risk(keepa_prod):
            return None

        asin = keepa_prod.get("asin", "UNKNOWN_ASIN")
        title = keepa_prod.get("title", "Product")

        return {
            "asin": asin,
            "title": title,
            "buy_box": buy_box_price,
            "monthly_drops": monthly_drops,
            "seller_count": live_sellers,
            "amazon_in_stock_pct": amazon_in_stock_rate
        }
