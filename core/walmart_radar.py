import time
from typing import Dict, Any, Optional
from core.walmart_cost_engine import WalmartCostEngine

class WalmartRadar:
    """
    Precision Walmart Velocity, Stockout & Margin Intelligence.
    Cross-references Keepa CSV tracking history with true-demand metrics
    and executes exact WFS/Referral calculations via WalmartCostEngine.
    """
    # Keepa CSV sub-array index for Walmart price history
    KEEPA_CSV_WALMART_INDEX = 11

    # Keepa time epoch starts on 2011-01-01 (minutes offset from Unix epoch)
    KEEPA_TIME_OFFSET_MINUTES = 21564000

    @classmethod
    def keepa_time_to_epoch(cls, keepa_minute: int) -> int:
        """Converts Keepa minute timestamp to standard Unix epoch seconds."""
        return (keepa_minute + cls.KEEPA_TIME_OFFSET_MINUTES) * 60

    @classmethod
    def analyze_walmart_opportunity(
        cls, 
        keepa_prod: Dict[str, Any], 
        wholesale_cost: float,
        monthly_drops: int
    ) -> Dict[str, Any]:
        """
        Evaluates real consumer demand, stockout recency, price parity,
        and all-in WFS profitability for an item.
        """
        csv_data = keepa_prod.get("csv", [])
        stats = keepa_prod.get("stats", {})
        
        # 1. Base Social Proof & Amazon Price Benchmark
        total_reviews = stats.get("reviewCount", 0)
        amz_bb_cents = stats.get("buyBoxPrice", -1)
        amz_buy_box = round(amz_bb_cents / 100.0, 2) if amz_bb_cents > 0 else 0.0

        # 2. Extract Physical Dimensions & Weight for Precision WFS Calculation
        package_weight_grams = keepa_prod.get("packageWeight", 453)  # Default: ~1 lb
        weight_lbs = max(0.2, round(package_weight_grams / 453.592, 2))
        
        # Dimensions in inches (Keepa stores in millimeters)
        pkg_len_mm = keepa_prod.get("packageLength", 152)  # Default ~6 inches
        pkg_w_mm = keepa_prod.get("packageWidth", 101)    # Default ~4 inches
        pkg_h_mm = keepa_prod.get("packageHeight", 50)    # Default ~2 inches
        
        len_in = max(1.0, round(pkg_len_mm / 25.4, 1))
        width_in = max(1.0, round(pkg_w_mm / 25.4, 1))
        height_in = max(1.0, round(pkg_h_mm / 25.4, 1))

        # 3. Inspect Keepa's Walmart Price History Array
        walmart_csv = csv_data[cls.KEEPA_CSV_WALMART_INDEX] if len(csv_data) > cls.KEEPA_CSV_WALMART_INDEX else None

        current_walmart_price = -1.0
        historical_walmart_price = -1.0
        last_seen_in_stock_timestamp = 0
        is_walmart_oos = True

        now_epoch = int(time.time())
        ninety_days_ago = now_epoch - (90 * 86400)

        if walmart_csv and len(walmart_csv) >= 2:
            # Check the most recent point in time
            last_price_cents = walmart_csv[-1]
            if last_price_cents > 0:
                current_walmart_price = round(last_price_cents / 100.0, 2)
                is_walmart_oos = False
            else:
                is_walmart_oos = True

            # Walk backward to find the last valid price and the timestamp when Walmart was in stock
            for i in range(len(walmart_csv) - 1, 0, -2):
                price_cents = walmart_csv[i]
                time_min = walmart_csv[i - 1]
                if price_cents > 0:
                    historical_walmart_price = round(price_cents / 100.0, 2)
                    last_seen_in_stock_timestamp = cls.keepa_time_to_epoch(time_min)
                    break

        # Verification check: Was this item active on Walmart within the last 90 days?
        was_recently_active = last_seen_in_stock_timestamp >= ninety_days_ago

        # 4. Target Selling Price Determination & Parity Protection
        # Protects against Walmart's price crawler suppressing listings priced way higher than Amazon
        benchmark_price = current_walmart_price if current_walmart_price > 0 else (
            historical_walmart_price if historical_walmart_price > 0 else amz_buy_box
        )
        
        if amz_buy_box > 0 and benchmark_price > (amz_buy_box * 1.15):
            target_walmart_price = round(amz_buy_box * 1.10, 2)
        else:
            target_walmart_price = benchmark_price

        # 5. Execute Precision Fee Engine (Referral, Tiered WFS, Tare Weight, Inbound Freight)
        category_hint = keepa_prod.get("binding", "beauty")
        wmt_financials = WalmartCostEngine.calculate_total_walmart_payout(
            sell_price=target_walmart_price,
            wholesale_cost=wholesale_cost,
            weight_lbs=weight_lbs,
            inbound_freight_per_unit=0.35,
            category=category_hint
        )

        walmart_profit = wmt_financials["net_profit"]
        walmart_roi = wmt_financials["roi"]

        # 6. Apply True-Demand Multi-Channel Gate
        # Item MUST have:
        # a) Minimum 30 verified monthly sales drops
        # b) Minimum 30 product reviews (not an unvetted ghost listing)
        # c) Positive Walmart ROI >= 35.0%
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
            "weight_lbs": weight_lbs,
            "wfs_fee": wmt_financials["wfs_fulfillment_fee"],
            "walral_fee": wmt_financials["walral_fee"],
            "walmart_roi": walmart_roi,
            "walmart_profit": walmart_profit,
            "has_true_demand": has_true_demand,
            "is_walmart_winner": is_walmart_winner,
            "channel_strategy": strategy
        }
