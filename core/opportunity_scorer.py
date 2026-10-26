from typing import Dict, Any, Tuple

class OpportunityScorer:
    """
    100-Point Composite Deal Scorer:
    - Base ROI & Economics: 40 pts
    - Demand Velocity & Supply Gaps: 30 pts
    - Competition Health: 20 pts
    - Payment Terms & Credit Card Cash Float: 10 pts
    """

    @classmethod
    def calculate_payment_bonus(cls, payment_terms: Dict[str, Any]) -> Tuple[int, str]:
        method = str(payment_terms.get("payment_method", "ACH")).upper()
        cc_fee = float(payment_terms.get("cc_surcharge_pct", 0.0))
        early_pay = float(payment_terms.get("early_pay_discount_pct", 0.0))

        if method == "CREDIT_CARD" and cc_fee == 0.0:
            return 10, "FREE_CC_FLOAT_AND_REWARDS (+10)"
        elif method == "TERMS_2_10_NET_30" or (method == "ACH" and early_pay > 0):
            return 8, "TRADE_CREDIT_NET30_TERMS (+8)"
        elif method == "CREDIT_CARD" and cc_fee <= 0.02:
            return 4, "LOW_FEE_CC_FLOAT (+4)"
        return 0, "STANDARD_PREPAY_OR_SURCHARGE (+0)"

    def score_product(
        self,
        keepa_product: Dict[str, Any],
        amz_roi: float,
        wm_roi: float,
        is_amz_gap: bool,
        is_wm_gap: bool,
        min_roi: float = 35.0,
        payment_terms: Dict[str, Any] = None
    ) -> Tuple[int, str, Dict[str, Any]]:
        payment_terms = payment_terms or {}
        score = 0

        # 1. Base Economics (Max 40 pts)
        best_roi = max(amz_roi, wm_roi)
        if best_roi >= 70.0:
            score += 40
        elif best_roi >= 50.0:
            score += 30
        elif best_roi >= min_roi:
            score += 20
        else:
            score += 5

        # 2. Demand Velocity & Gaps (Max 30 pts)
        stats = keepa_product.get("stats", {}) or {}
        drops = stats.get("salesRankDrops30", 0) or 0
        if is_amz_gap and is_wm_gap:
            score += 30
        elif is_amz_gap or is_wm_gap:
            score += 20
        elif drops >= 40:
            score += 15
        elif drops >= 20:
            score += 10

        # 3. Competition Health (Max 20 pts)
        fba_sellers = stats.get("current", [0] * 11)[10] or 0
        if fba_sellers <= 2:
            score += 20
        elif fba_sellers <= 5:
            score += 15
        elif fba_sellers <= 8:
            score += 10

        # 4. Working Capital & Float Bonus (Max 10 pts)
        pay_points, pay_reason = self.calculate_payment_bonus(payment_terms)
        score += pay_points
        score = min(score, 100)

        # Grade Assignment
        if score >= 90:
            grade = "A++" if (is_amz_gap and is_wm_gap) else "A+"
        elif score >= 80:
            grade = "A"
        elif score >= 65:
            grade = "B"
        else:
            grade = "C"

        breakdown = {
            "fba_sellers": fba_sellers,
            "monthly_drops": drops,
            "is_dual_gap": (is_amz_gap and is_wm_gap),
            "payment_bonus_points": pay_points,
            "payment_benefit": pay_reason
        }
        return score, grade, breakdown
