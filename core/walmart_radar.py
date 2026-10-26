import time
from typing import Dict, Any, Optional
from core.walmart_cost_engine import WalmartCostEngine

class WalmartRadar:
    """
    Walmart Velocity, Stockout & Compliance Intelligence.
    Enforces the 15% Amazon Parity Cap, applies preferred FedEx WFS inbound rates,
    and flags bag warning requirements.
    """
    KEEPA_CSV_WALMART_INDEX = 11
    KEEPA_TIME_OFFSET_MINUTES = 21564000

    @classmethod
    def keepa_time_to_epoch(cls, keepa_minute: int) -> int:
        return (keepa_minute + cls.KEEPA_TIME_OFFSET_MINUTES) * 60

    @classmethod
    def calculate_wfs_inbound_shipping(cls, weight_lbs: float) -> float:
        """
        Calculates Walmart Preferred Carrier (FedEx Ground) discounted rates.
        From Parkland, FL to Southeast WFS FCs (e.g., Davenport, FL or Atlanta, GA),
        rates average ~$0.25 - $0.32/lb ($9-$14 per 40 lb carton).
        """
        weight = max(0.2, weight_lbs)
        if weight <= 1.0:
            return 0.22
        elif weight <= 3.0:
            return 0.45
        else:
            # Baseline bulk rate per lb via WPC FedEx
            return round(weight * 0.28, 2)

    @classmethod
    def analyze_walmart_opportunity(
        cls, 
        keepa_prod: Dict[str, Any], 
        wholesale_cost: float,
        monthly_drops: int
    ) -> Dict[str, Any]:
        csv_data = keepa_prod.get("csv", [])
        stats = keepa_prod.get("stats", {})
        
        total_reviews = stats.get("reviewCount", 0)
        amz_bb_cents = stats.get("buyBoxPrice", -1)
        amz_buy_box = round(amz_bb_cents / 100.0, 2) if amz_bb_cents > 0 else 0.0

        # Physical Dimensions & Weight
        package_weight_grams = keepa_prod.get("packageWeight", 453)
        weight_lbs = max(0.2, round(package_weight_grams / 453.592, 2))
        
        pkg_len_mm = keepa_prod.get("packageLength", 152)
        pkg_w_mm = keepa_prod.get("packageWidth", 101)
        pkg_h_mm = keepa_prod.get("packageHeight", 50)
        
        len_in = max(1.0, round(pkg_len_mm / 25.4, 1))
        width_in = max(1.0, round(pkg_w_mm / 25.4, 1))
        height_in = max(1.0, round(pkg_h_mm / 25.4, 1))

        # Check Keepa's Walmart Price History
        walmart_csv = csv_data[cls.KEEPA_CSV_WALMART_INDEX] if len(csv_data) > cls.KEEPA_CSV_WALMART_INDEX else None

        current_walmart_price = -1.0
        historical_walmart_price = -1.0
        last_seen_in_stock_timestamp = 0
        is_walmart_oos = True

        now_epoch = int(time.time())
        ninety_days_ago = now_epoch - (90 * 86400)

        if walmart_csv and len(walmart_csv) >= 2:
            last_price_cents = walmart_csv[-1]
            if last_price_cents > 0:
                current_walmart_price = round(last_price_cents / 100.0, 2)
                is_walmart_oos = False
            else:
                is_walmart_oos = True

            for i in range(len(walmart_csv) - 1, 0, -2):
                price_cents = walmart_csv[i]
                time_min = walmart_csv[i - 1]
                if price_cents > 0:
                    historical_walmart_price = round(price_cents / 100.0, 2)
                    last_seen_in_stock_timestamp = cls.keepa_time_to_epoch(time_min)
                    break

        was_recently_active = last_seen_in_stock_timestamp >= ninety_days_ago

        # -------------------------------------------------------------
        # RULE 1: THE PRICE MATCHING TRAP (15% PARITY CAP)
        # -------------------------------------------------------------
        raw_target_price = current_walmart_price if current_walmart_price > 0 else (
            historical_walmart_price if historical_walmart_price > 0 else amz_buy_box
        )

        price_parity_applied = False
        target_walmart_price = raw_target_price

        # If Walmart is OOS and target exceeds Amazon Buy Box by > 15%, clamp to 1.15x
        if amz_buy_box > 0 and raw_target_price > (amz_buy_box * 1.15):
            target_walmart_price = round(amz_buy_box * 1.15, 2)
            price_parity_applied = True

        # -------------------------------------------------------------
        # RULE 2: WFS FEDEX DISCOUNT INBOUND SHIPPING
        # -------------------------------------------------------------
        wfs_inbound_freight = cls.calculate_wfs_inbound_shipping(weight_lbs)

        # Execute Precision Fee Engine
        category_hint = keepa_prod.get("binding", "beauty")
        wmt_financials = WalmartCostEngine.calculate_total_walmart_payout(
            sell_price=target_walmart_price,
            wholesale_cost=wholesale_cost,
            weight_lbs=weight_lbs,
            inbound_freight_per_unit=wfs_inbound_freight,
            category=category_hint
        )

        walmart_profit = wmt_financials["net_profit"]
        walmart_roi = wmt_financials["roi"]

        # -------------------------------------------------------------
        # RULE 3: PACKAGING & WARNING LABEL COMPLIANCE
        # -------------------------------------------------------------
        # Amazon requires warning if opening >= 5 inches; Walmart requires if >= 12 inches.
        # Standardizing on 1.5 mil pre-printed bags covers both.
        max_dimension = max(len_in, width_in, height_in)
        needs_poly_warning_bag = max_dimension >= 5.0

        # True-Demand Evaluation
        has_true_demand = (monthly_drops >= 30) and (total_reviews >= 30)
        is_fresh_stockout = is_walmart_oos and was_recently_active

        is_walmart_winner = False
        strategy = "AMAZON_ONLY"

        if has_true_demand and walmart_roi >= 35.0:
            if is_fresh_stockout:
                is_walmart_winner = True
                strategy = "WALMART_HIGH_DEMAND_OOS"
            elif not is_walmart_oos and current_walmart_price > wholesale_cost * 1.5:
                is_walmart_winner = True
                strategy = "DUAL_CHANNEL_SPLIT"

        return {
            "walmart_oos": is_walmart_oos,
            "was_recently_active": was_recently_active,
            "walmart_current_price": current_walmart_price,
            "walmart_hist_price": historical_walmart_price,
            "walmart_target_price": target_walmart_price,
            "price_parity_capped": price_parity_applied,
            "weight_lbs": weight_lbs,
            "wfs_inbound_freight": wfs_inbound_freight,
            "wfs_fee": wmt_financials["wfs_fulfillment_fee"],
            "walral_fee": wmt_financials["walral_fee"],
            "walmart_roi": walmart_roi,
            "walmart_profit": walmart_profit,
            "has_true_demand": has_true_demand,
            "is_walmart_winner": is_walmart_winner,
            "channel_strategy": strategy,
            "prep_note": "1.5mil Suffocation Poly Bag" if needs_poly_warning_bag else "Factory Box / Direct Scan"
        }
