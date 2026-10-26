import time
from typing import Dict, Any, Optional

class WalmartRadar:
    """
    Advanced Walmart Velocity & Out-Of-Stock Intelligence.
    Filters out dead/discontinued SKUs and identifies high-demand items
    where Walmart 1P recently stocked out, creating an attractive WFS opportunity.
    """
    KEEPA_CSV_WALMART_INDEX = 11

    # Keepa time offset constant (Keepa epoch starts at 2011-01-01)
    KEEPA_TIME_OFFSET_MINUTES = 21564000

    @classmethod
    def keepa_time_to_epoch(cls, keepa_minute: int) -> int:
        return (keepa_minute + cls.KEEPA_TIME_OFFSET_MINUTES) * 60

    @classmethod
    def analyze_walmart_opportunity(
        cls, 
        keepa_prod: Dict[str, Any], 
        wholesale_cost: float,
        monthly_drops: int
    ) -> Dict[str, Any]:
        """
        Evaluates real consumer demand, stockout recency, and profitability
        before clearing an item as a Walmart WFS opportunity.
        """
        csv_data = keepa_prod.get("csv", [])
        stats = keepa_prod.get("stats", {})
        
        # 1. Base consumer proof: Review count check
        total_reviews = stats.get("reviewCount", 0)
        
        # Check if Walmart tracking array exists in Keepa
        walmart_csv = csv_data[cls.KEEPA_CSV_WALMART_INDEX] if len(csv_data) > cls.KEEPA_CSV_WALMART_INDEX else None

        current_walmart_price = -1.0
        historical_walmart_price = -1.0
        last_seen_in_stock_timestamp = 0
        is_walmart_oos = True

        now_epoch = int(time.time())
        ninety_days_ago = now_epoch - (90 * 86400)

        if walmart_csv and len(walmart_csv) >= 2:
            # Last recorded price in the series
            last_price_cents = walmart_csv[-1]
            if last_price_cents > 0:
                current_walmart_price = round(last_price_cents / 100.0, 2)
                is_walmart_oos = False
            else:
                is_walmart_oos = True

            # Walk backward to find when Walmart was last in stock and at what price
            # Array format: [time_0, price_0, time_1, price_1, ...]
            for i in range(len(walmart_csv) - 1, 0, -2):
                price_cents = walmart_csv[i]
                time_min = walmart_csv[i - 1]
                if price_cents > 0:
                    historical_walmart_price = round(price_cents / 100.0, 2)
                    last_seen_in_stock_timestamp = cls.keepa_time_to_epoch(time_min)
                    break

        # Check recency: Was it in stock within the last 90 days?
        was_recently_active = last_seen_in_stock_timestamp >= ninety_days_ago

        # 2. Benchmark target selling price on Walmart
        stats_bb = stats.get("buyBoxPrice", -1)
        amz_buy_box = round(stats_bb / 100.0, 2) if stats_bb > 0 else 0.0

        target_walmart_price = current_walmart_price if current_walmart_price > 0 else (
            historical_walmart_price if historical_walmart_price > 0 else amz_buy_box
        )

        # Walmart Commission (15%) + Average WFS Standard Fulfillment ($3.45)
        wfs_fulfillment_est = 3.45
        walmart_net_payout = target_walmart_price * 0.85 - wfs_fulfillment_est
        walmart_profit = round(walmart_net_payout - wholesale_cost, 2)
        walmart_roi = round((walmart_profit / wholesale_cost) * 100.0, 1) if wholesale_cost > 0 else 0.0

        # --- TRUE DEMAND CRITERIA ---
        # 1. Product must move (minimum 30 monthly sales drops)
        # 2. Product must have legitimate social proof (>= 30 reviews)
        # 3. If OOS on Walmart, it must be a RECENT stockout (in stock within 90 days), not dead stock
        # 4. Target price must hit our minimum ROI hurdle (>= 35%)
        has_true_demand = (monthly_drops >= 30) and (total_reviews >= 30)
        is_fresh_stockout = is_walmart_oos and was_recently_active

        is_walmart_winner = False
        strategy = "AMAZON_ONLY"

        if has_true_demand and walmart_roi >= 35.0:
            if is_fresh_stockout:
                is_walmart_winner = True
                strategy = "WALMART_HIGH_DEMAND_OOS"
            elif not is_walmart_oos and current_walmart_price > wholesale_cost * 1.5:
                # In stock, but market price allows strong margin
                is_walmart_winner = True
                strategy = "DUAL_CHANNEL_SPLIT"

        return {
            "walmart_oos": is_walmart_oos,
            "was_recently_active": was_recently_active,
            "walmart_current_price": current_walmart_price,
            "walmart_hist_price": historical_walmart_price,
            "walmart_target_price": target_walmart_price,
            "walmart_roi": walmart_roi,
            "walmart_profit": walmart_profit,
            "has_true_demand": has_true_demand,
            "is_walmart_winner": is_walmart_winner,
            "channel_strategy": strategy
        }
