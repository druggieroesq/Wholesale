"""
tools/master_vacuum_engine.py
Central Financial & Operational Gatekeeper.
Dynamically reads MIN_ROI_FLOOR, prep baselines, and fee structures from .env.
Enforces Layer 0 IP Guard, physical ceilings, and zero-supply vacuum criteria.
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env", override=True)

from tools.ip_guard import is_brand_blocked

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MasterVacuumEngine")

MAX_ALLOWED_WEIGHT_LBS = float(os.getenv("MAX_UNIT_WEIGHT_LBS", 45.0))
MAX_ALLOWED_DIMENSION_INCHES = float(os.getenv("MAX_BOX_DIMENSION_INCHES", 24.0))


class MasterVacuumEngine:
    """
    Evaluates deals against zero-supply inventory vacuum and operational criteria:
    - Layer 0 IP Guard enforcement (659+ protected brands).
    - Zero/Low FBA Supply (FBA sellers <= 1, Amazon Retail OOS).
    - Dynamic ROI Floor and prep fee loaded directly from .env.
    - 45.0 lb max unit weight / 24.0 inch max box dimension.
    - Amazon Referral Fee (15%) + Dynamic Tiered FBA fee.
    """
    _raw_env_roi = float(os.getenv("MIN_ROI_FLOOR", os.getenv("MIN_ROI", 1.00)))
    DEFAULT_ROI_FLOOR_PCT = _raw_env_roi * 100.0 if _raw_env_roi <= 2.0 else _raw_env_roi
    DEFAULT_PREP_FEE = float(os.getenv("PARKLAND_PREP_FEE_UNIT", 0.06))
    AMAZON_REFERRAL_PCT = float(os.getenv("AMAZON_REFERRAL_PCT", 0.15))

    def __init__(self, prep_override: Optional[float] = None, roi_override: Optional[float] = None):
        self.prep_fee = prep_override if prep_override is not None else self.DEFAULT_PREP_FEE
        self.min_roi_floor = roi_override if roi_override is not None else self.DEFAULT_ROI_FLOOR_PCT

    @staticmethod
    def validate_physical_ceilings(candidate: Dict[str, Any]) -> bool:
        weight = float(candidate.get("package_weight_lbs", 0.0))
        if weight > MAX_ALLOWED_WEIGHT_LBS:
            logger.debug(f"Breached weight: {candidate.get('asin')} is {weight} lbs.")
            return False

        max_dim = float(candidate.get("max_dimension_inches", 0.0))
        if max_dim > MAX_ALLOWED_DIMENSION_INCHES:
            logger.debug(f"Breached dimension: {candidate.get('asin')} is {max_dim} in.")
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
        # 1. Physical limits check
        if not self.validate_physical_ceilings(candidate):
            return None

        # 2. Layer 0 IP Guard Check
        brand = candidate.get("brand", "")
        title = candidate.get("title", "")
        vendor = candidate.get("vendor", "")
        blocked, reason = is_brand_blocked(brand=brand, title=title, vendor=vendor)
        if blocked:
            logger.debug(f"Skipping {candidate.get('asin')}: {reason}")
            return None

        # 3. Supply Vacuum Gate (Amazon Retail must be OOS, FBA sellers <= 1)
        fba_sellers = candidate.get("fba_sellers", candidate.get("live_fba_count", 0))
        amazon_in_stock = candidate.get("amazon_in_stock", False)
        if amazon_in_stock or fba_sellers > 1:
            logger.debug(f"Not a supply vacuum: {candidate.get('asin')} (Amazon in stock: {amazon_in_stock}, FBA: {fba_sellers})")
            return None

        wholesale_cost = float(candidate.get("cost", candidate.get("wholesale_cost", 0.0)))
        amz_price = float(candidate.get("amz_price", candidate.get("amazon_price", 0.0)))

        if wholesale_cost <= 0 or amz_price <= 0:
            return None

        weight_lbs = float(candidate.get("package_weight_lbs", 1.5))
        if weight_lbs <= 0:
            weight_lbs = 1.5

        # 4. Amazon & Landed Cost Breakdown
        referral_fee = amz_price * self.AMAZON_REFERRAL_PCT
        fba_fee = self.estimate_fba_fee(weight_lbs)
        total_amazon_fees = referral_fee + fba_fee

        inbound_freight_est = candidate.get("inbound_freight", round(max(0.65, weight_lbs * 0.40), 2))
        total_prep_cost = self.prep_fee + inbound_freight_est

        total_landed_cost = wholesale_cost + total_prep_cost
        net_profit = amz_price - total_amazon_fees - total_landed_cost
        roi_pct = (net_profit / total_landed_cost) * 100.0

        # Strict Dynamic ROI Floor Check (from .env)
        if roi_pct < self.min_roi_floor:
            return None

        # 5. Vacuum Quadrant Matrix
        if fba_sellers == 0 and roi_pct >= (self.min_roi_floor * 1.5):
            quadrant = "Q1_TOTAL_VACUUM_SURGE"
        elif fba_sellers == 0:
            quadrant = "Q2_ZERO_SUPPLY_MONOPOLY"
        elif roi_pct >= (self.min_roi_floor * 1.5):
            quadrant = "Q3_HIGH_MARGIN_HARVEST"
        else:
            quadrant = "Q4_SELECTIVE_EXPANSION"

        return {
            "asin": candidate.get("asin", "UNKNOWN"),
            "upc": candidate.get("upc", ""),
            "title": title,
            "brand": brand,
            "mpn": candidate.get("mpn", ""),
            "vendor": vendor,
            "wholesale_cost": round(wholesale_cost, 2),
            "amazon_price": round(amz_price, 2),
            "total_landed_cost": round(total_landed_cost, 2),
            "net_profit": round(net_profit, 2),
            "roi_pct": round(roi_pct, 1),
            "quadrant": quadrant,
            "fba_sellers": fba_sellers,
            "fulfillment_center": "LOCAL_HQ",
            "destination": "Parkland, FL Station",
            "prep_fee_unit": self.prep_fee,
            "package_weight_lbs": weight_lbs
        }

    def process_catalog(self, raw_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        viable = []
        for c in raw_candidates:
            evaluated = self.evaluate_deal(c)
            if evaluated:
                viable.append(evaluated)

        viable.sort(key=lambda x: x["roi_pct"], reverse=True)
        logger.info(f"Processed {len(raw_candidates)} items -> {len(viable)} passed vacuum, IP, and ROI filters (Floor: {self.min_roi_floor}%).")
        return viable


if __name__ == "__main__":
    engine = MasterVacuumEngine()
    test_batch = [
        {
            "asin": "B08N5XYZ12",
            "mpn": "VALVE-50",
            "title": "Brass Shut-Off Valve",
            "brand": "ProFlow",
            "cost": 11.50,
            "amz_price": 38.99,
            "fba_sellers": 0,
            "amazon_in_stock": False,
            "package_weight_lbs": 1.2,
            "max_dimension_inches": 6.0
        }
    ]
    approved = engine.process_catalog(test_batch)
    print(f"Viable Deals: {len(approved)}")
