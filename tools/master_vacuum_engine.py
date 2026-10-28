import os
import sys
import json
import logging
from typing import Dict, Any, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MasterVacuumEngine")

MAX_ALLOWED_WEIGHT_LBS = 45.0
MAX_ALLOWED_DIMENSION_INCHES = 24.0

class MasterVacuumEngine:
    """
    Evaluates deals against marketplace and operational durability criteria:
    - 45.0 lb max unit weight (safety threshold below Amazon 50.0 lb ceiling)
    - 24.0 inch max box dimension
    - Amazon Referral Fee (15%) + FBA Tiered Weights
    - Inbound freight & Local Parkland Self-Prep ($0.35/unit)
    - Strict 50% ROI Floor
    - 4-Quadrant Priority Matrix
    """
    LOCAL_PREP_FEE = 0.35
    AMAZON_REFERRAL_PCT = 0.15
    MINIMUM_ROI_FLOOR = 50.0

    def __init__(self, prep_override: Optional[float] = None):
        self.prep_fee = prep_override if prep_override is not None else self.LOCAL_PREP_FEE

    @staticmethod
    def validate_physical_ceilings(candidate: Dict[str, Any]) -> bool:
        weight = float(candidate.get("package_weight_lbs", 0.0))
        if weight > MAX_ALLOWED_WEIGHT_LBS:
            logger.debug(f"Physical ceiling breached: {candidate.get('asin')} is {weight} lbs (> {MAX_ALLOWED_WEIGHT_LBS} lbs).")
            return False

        max_dim = float(candidate.get("max_dimension_inches", 0.0))
        if max_dim > MAX_ALLOWED_DIMENSION_INCHES:
            logger.debug(f"Physical ceiling breached: {candidate.get('asin')} has dimension {max_dim} in (> {MAX_ALLOWED_DIMENSION_INCHES} in).")
            return False

        return True

    @staticmethod
    def estimate_fba_fee(weight_lbs: float) -> float:
        if weight_lbs <= 1.0:
            return 3.86
        elif weight_lbs <= 2.0:
            return 5.40
        elif weight_lbs <= 3.0:
            return 6.10
        else:
            return round(6.10 + (weight_lbs - 3.0) * 0.45, 2)

    def evaluate_deal(self, candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.validate_physical_ceilings(candidate):
            return None

        asin = candidate.get("asin", "UNKNOWN")
        wholesale_cost = float(candidate.get("cost", candidate.get("wholesale_cost", 0.0)))
        amz_price = float(candidate.get("amz_price", candidate.get("amazon_price", 0.0)))

        if wholesale_cost <= 0 or amz_price <= 0:
            return None

        weight_lbs = float(candidate.get("package_weight_lbs", 1.5))
        if weight_lbs <= 0:
            weight_lbs = 1.5

        referral_fee = amz_price * self.AMAZON_REFERRAL_PCT
        fba_fee = self.estimate_fba_fee(weight_lbs)
        total_amazon_fees = referral_fee + fba_fee

        inbound_freight_est = round(max(0.65, weight_lbs * 0.40), 2)
        total_prep_cost = self.prep_fee + inbound_freight_est

        total_landed_cost = wholesale_cost + total_prep_cost
        net_profit = amz_price - total_amazon_fees - total_landed_cost
        roi_pct = (net_profit / total_landed_cost) * 100.0

        if roi_pct < self.MINIMUM_ROI_FLOOR:
            return None

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
            "sales_rank": sales_rank,
            "package_weight_lbs": weight_lbs
        }

    def process_catalog(self, raw_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        viable = []
        for c in raw_candidates:
            evaluated = self.evaluate_deal(c)
            if evaluated:
                viable.append(evaluated)

        viable.sort(key=lambda x: x["roi_pct"], reverse=True)
        logger.info(f"Processed {len(raw_candidates)} items -> {len(viable)} passed physical & ROI filters.")
        return viable


if __name__ == "__main__":
    engine = MasterVacuumEngine()
    test_batch = [
        {
            "asin": "B08N5XYZ12",
            "mpn": "VALVE-50",
            "title": "Brass Shut-Off Valve",
            "cost": 11.50,
            "amz_price": 36.99,
            "vendor": "global_industrial",
            "est_sales_rank": 22000,
            "package_weight_lbs": 1.2,
            "max_dimension_inches": 6.0
        },
        {
            "asin": "B01OVERWEIGHT",
            "mpn": "HEAVY-SACK-50LB",
            "title": "50 lb Bulk Sack",
            "cost": 25.00,
            "amz_price": 95.00,
            "vendor": "atlantic_spice",
            "est_sales_rank": 10000,
            "package_weight_lbs": 52.0,
            "max_dimension_inches": 20.0
        }
    ]
    approved = engine.process_catalog(test_batch)
    print(f"Viable Deals: {len(approved)}")
