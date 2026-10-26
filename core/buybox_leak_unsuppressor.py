from typing import Dict, Any

class BuyBoxLeakUnsuppressor:
    """
    Identifies suppressed Buy Box listings and calculates target unsuppression pricing.
    Recovers sales where Amazon suppressed the Buy Box due to perceived algorithmic price gouging.
    """

    def __init__(self):
        pass

    def evaluate_suppression(self, keepa_product: Dict[str, Any]) -> Dict[str, Any]:
        """
        Checks if the current Buy Box is suppressed (-1) and estimates a profitable unsuppress price
        based on 90-day historical averages.
        """
        keepa_product = keepa_product or {}
        stats = keepa_product.get("stats", {}) or {}
        curr = stats.get("current", [])
        avg90 = stats.get("avg90", [])

        # Index 18 is the current Buy Box price in cents
        bb_cents = curr[18] if len(curr) > 18 else -1
        is_suppressed = (bb_cents == -1) or (bb_cents is None)

        target_price = 0.0
        if is_suppressed:
            # Fallback priority: 90-day historical Buy Box -> current lowest 3P new offer
            hist_cents = (
                avg90[18] 
                if len(avg90) > 18 and avg90[18] and avg90[18] > 0 
                else (curr[1] if len(curr) > 1 and curr[1] and curr[1] > 0 else 0)
            )
            if hist_cents > 0:
                # 2% discount below ceiling to trigger unsuppression
                target_price = round((hist_cents / 100.0) * 0.98, 2)

        return {
            "is_suppressed_opportunity": is_suppressed and (target_price > 0),
            "target_unsuppress_price": target_price
        }
