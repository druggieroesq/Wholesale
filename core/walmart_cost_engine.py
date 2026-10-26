import math
from typing import Dict, Any

class WalmartCostEngine:
    """
    Precision fee calculator for Walmart Marketplace and WFS (Walmart Fulfillment Services).
    Incorporates category referral brackets, weight tiers, and low-price surcharges.
    """

    @classmethod
    def calculate_referral_fee(cls, sell_price: float, category: str = "beauty") -> float:
        """
        Walmart referral fee schedule:
        - Beauty / Health / Personal Care: 8% if <= $10, 15% if > $10
        - Grocery: 8% if <= $15, 15% if > $15
        - General Merchandise: Flat 15%
        """
        cat = category.lower()
        if any(b in cat for b in ["beauty", "personal", "health"]):
            rate = 0.08 if sell_price <= 10.00 else 0.15
        elif "grocery" in cat:
            rate = 0.08 if sell_price <= 15.00 else 0.15
        else:
            rate = 0.15

        return round(sell_price * rate, 2)

    @classmethod
    def calculate_wfs_fulfillment_fee(
        cls, 
        unit_weight_lbs: float, 
        length_in: float = 6.0, 
        width_in: float = 4.0, 
        height_in: float = 2.0, 
        retail_price: float = 15.00
    ) -> float:
        """
        Calculates exact WFS pick, pack, and delivery cost.
        Applies packaging tare weight and low-ticket retail surcharges.
        """
        unit_weight = max(0.1, unit_weight_lbs)

        # Shipping weight determination
        if unit_weight <= 1.0:
            shipping_weight = unit_weight
        else:
            dim_weight = (length_in * width_in * height_in) / 139.0
            greater_weight = max(unit_weight, dim_weight)
            # Add 0.25 lb packaging allowance and round up
            shipping_weight = math.ceil(greater_weight + 0.25)

        # Base rate card lookup
        if shipping_weight <= 1.0:
            fee = 3.45
        elif shipping_weight <= 2.0:
            fee = 4.95
        elif shipping_weight <= 3.0:
            fee = 5.45
        elif shipping_weight <= 20.0:
            fee = 5.75 + (0.40 * (shipping_weight - 4.0))
        else:
            # Over 20 lbs
            fee = 15.55 + (0.40 * (shipping_weight - 21.0))

        # Surcharges: Items priced under $10 incur an extra $1.00 WFS fee
        if retail_price < 10.00:
            fee += 1.00

        return round(fee, 2)

    @classmethod
    def calculate_total_walmart_payout(
        cls, 
        sell_price: float, 
        wholesale_cost: float, 
        weight_lbs: float = 1.0, 
        inbound_freight_per_unit: float = 0.35,
        category: str = "beauty"
    ) -> Dict[str, float]:
        """
        Calculates all-in net profit and ROI for Walmart WFS.
        """
        referral_fee = cls.calculate_referral_fee(sell_price, category)
        wfs_fee = cls.calculate_wfs_fulfillment_fee(
            unit_weight_lbs=weight_lbs, 
            retail_price=sell_price
        )
        
        total_costs = wholesale_cost + referral_fee + wfs_fee + inbound_freight_per_unit
        net_profit = round(sell_price - total_costs, 2)
        roi = round((net_profit / wholesale_cost) * 100.0, 1) if wholesale_cost > 0 else 0.0

        return {
            "walral_fee": referral_fee,
            "wfs_fulfillment_fee": wfs_fee,
            "net_profit": net_profit,
            "roi": roi
        }
