class FBACalculator:
    @staticmethod
    def calculate_fulfillment_fee(weight_lbs: float) -> float:
        """Standard Amazon FBA fulfillment fee schedule."""
        if weight_lbs <= 0.25:
            return 3.22
        elif weight_lbs <= 0.5:
            return 3.40
        elif weight_lbs <= 0.75:
            return 3.58
        elif weight_lbs <= 1.0:
            return 3.77
        elif weight_lbs <= 1.5:
            return 4.42
        elif weight_lbs <= 2.0:
            return 4.95
        else:
            # $4.95 + $0.40 per lb above 2 lbs
            return round(4.95 + ((weight_lbs - 2.0) * 0.40), 2)

    @classmethod
    def calculate_net_economics(cls, buy_box: float, unit_cost: float, weight_lbs: float = 1.0, is_beauty: bool = True) -> dict:
        if buy_box <= 0:
            return {"net_profit": 0, "roi": 0, "margin": 0}

        # Beauty referral fee discount for sub-$10 products
        if is_beauty and buy_box <= 10.0:
            referral_rate = 0.08
        else:
            referral_rate = 0.15

        referral_fee = round(buy_box * referral_rate, 2)
        fba_fee = cls.calculate_fulfillment_fee(weight_lbs)
        inbound_ups_shipping = round(weight_lbs * 0.30, 2)  # Avg $0.30/lb SPD rate

        net_profit = round(buy_box - (unit_cost + referral_fee + fba_fee + inbound_ups_shipping), 2)
        roi = round((net_profit / unit_cost) * 100, 1) if unit_cost > 0 else 0
        margin = round((net_profit / buy_box) * 100, 1)

        return {
            "net_profit": net_profit,
            "roi": roi,
            "margin": margin,
            "referral_fee": referral_fee,
            "fba_fee": fba_fee
        }
