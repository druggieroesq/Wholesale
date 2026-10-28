import os
import sys
import json
import logging
from typing import Dict, Any, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MasterVacuumEngine")

class MasterVacuumEngine:
    """
    Evaluates candidate deals against marketplace durability rules:
    - 50% ROI floor
    - Amazon Referral Fee (15% standard) + FBA Tiered Weights
    - Inbound freight & Local Parkland Self-Prep ($0.35/unit)
    - 4-Quadrant Priority Matrix
    """
    LOCAL_PREP_FEE = 0.35
    AMAZON_REFERRAL_PCT = 0.15
    MINIMUM_ROI_FLOOR = 50.0  # Strict 50% ROI threshold

    def __init__(self, prep_override: Optional[float] = None):
        self.prep_fee = prep_override if prep_override is not None else self.LOCAL_PREP_FEE

    def estimate_fba_fee(self, amz_price: float, est_weight_lbs: float = 1.5) -> float:
        # Standard tiered fulfillment estimate
        if est_weight_lbs <= 1.0:
            return 3.86
        elif est_weight_lbs <= 2.0:
            return 5.40
        elif est_weight_lbs <= 3.0:
            return 6.10
        else:
            return 6.10 + (est_weight_lbs - 3.0) * 0.45

    def evaluate_deal(self, candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        asin = candidate.get("asin", "UNKNOWN")
        wholesale_cost = float(candidate.get("cost", candidate.get("wholesale_cost", 0.0)))
        amz_price = float(candidate.get("amz_price", candidate.get("amazon_price", 0.0)))
        
        if wholesale_cost <= 0 or amz_price <= 0:
            return None

        # Calculate Amazon marketplace overhead
        referral_fee = amz_price * self.AMAZON_REFERRAL_PCT
        fba_fee = self.estimate_fba_fee(amz_price)
        total_amazon_fees = referral_fee + fba_fee

        # Inbound handling & self-prep cost
        inbound_shipping_est = 0.65  # Average inbound freight per unit to Parkland HQ
        total_prep_cost = self.prep_fee + inbound_shipping_est

        # Net profit and return on investment
        total_landed_cost = wholesale_cost + total_prep_cost
        net_profit = amz_price - total_amazon_fees - total_landed_cost
        roi_pct = (net_profit / total_landed_cost) * 100.0

        if roi_pct < self.MINIMUM_ROI_FLOOR:
            logger.debug(f"Rejecting {asin}: ROI {roi_pct:.1f}% below {self.MINIMUM_ROI_FLOOR}% floor.")
            return None

        # 4-Quadrant Classification
        sales_rank = candidate.get("est_sales_rank", 50000)
        if sales_rank < 30000 and roi_pct >= 70.0:
            quadrant = "Q1_MONOPOLY_SPRINT"
        elif sales_rank < 30000:
            quadrant = "Q2_HIGH_VELOCITY_STABLE"
        elif roi_pct >= 75.0:
            quadrant = "Q3_HIGH_MARGIN_HARVEST"
        else:
            quadrant = "Q4_SELECTIVE_EXPANSION"

        return {
            "asin": asin,
            "title": candidate.get("title", "Industrial Supply Item"),
            "mpn": candidate.get("mpn", ""),
            "vendor": candidate.get("vendor", "global_industrial"),
            "wholesale_cost": round(wholesale_cost, 2),
            "amazon_price": round(amz_price, 2),
            "total_landed_cost": round(total_landed_cost, 2),
            "net_profit": round(net_profit, 2),
            "roi_pct": round(roi_pct, 1),
            "quadrant": quadrant,
            "fulfillment_center": "LOCAL_HQ",
            "destination": "Parkland, FL Station",
            "prep_fee_unit": self.prep_fee,
            "sales_rank": sales_rank
        }

    def process_catalog(self, raw_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        viable = []
        for c in raw_candidates:
            evaluated = self.evaluate_deal(c)
            if evaluated:
                viable.append(evaluated)

        # Sort descending by ROI
        viable.sort(key=lambda x: x["roi_pct"], reverse=True)
        logger.info(f"Evaluated {len(raw_candidates)} items -> {len(viable)} pass ROI floor & fee thresholds.")
        return viable


if __name__ == "__main__":
    engine = MasterVacuumEngine()
    test_batch = [
        {"asin": "B08N5XYZ12", "mpn": "VALVE-50", "title": "Brass Shut-Off Valve", "cost": 11.50, "amz_price": 36.99, "vendor": "global_industrial", "est_sales_rank": 22000},
        {"asin": "B01TESTFAIL", "mpn": "LOW-MARGIN", "title": "Standard Nut Pack", "cost": 10.00, "amz_price": 14.99, "vendor": "midland_metal", "est_sales_rank": 80000}
    ]
    approved = engine.process_catalog(test_batch)
    print(f"\nViable Targets: {len(approved)}")
    for item in approved:
        print(f"[{item['quadrant']}] {item['asin']} | Cost: ${item['wholesale_cost']} | Price: ${item['amazon_price']} | ROI: {item['roi_pct']}% | Net: ${item['net_profit']}")
