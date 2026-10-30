"""
tools/reverse_sourcing_engine.py
Reverse-Sourcing Engine: ASIN -> Brand/MPN -> Target Wholesale Ceiling -> Wholesaler Query.
Identifies high-velocity MRO ASINs on Amazon and maps them to distributor targets.
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ReverseSourcing")

DATA_DIR = ROOT_DIR / "data"
CATALOG_DIR = DATA_DIR / "catalogs"
OUTPUT_DIR = DATA_DIR / "reverse_leads"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Sourcing Thresholds
MIN_MONTHLY_VELOCITY = 15          # At least 15 sales/month
MAX_COMPETING_FBA = 4              # Avoid crowded listings
MIN_ESTIMATED_ROI = 1.0            # 100% ROI Floor
PREP_FEE_BASELINE = 0.06           # $0.06 prep baseline (materials only)
AMAZON_REFERRAL_PCT = 0.15         # Standard MRO referral fee baseline
ESTIMATED_FBA_FULFILLMENT = 4.75   # Default standard Tier 1/2 MRO weight tier


def calculate_max_target_cost(buybox_price: float, fba_fee: float = ESTIMATED_FBA_FULFILLMENT) -> float:
    """
    Computes maximum wholesale buy price allowable to achieve >= 100% Net ROI.
    Net Profit = BuyBox - (BuyBox * Referral) - FBA_Fee - Prep_Fee - Wholesale_Cost
    Target ROI = Net Profit / Wholesale_Cost >= 1.0
    Wholesale_Cost <= (BuyBox * (1 - Referral) - FBA_Fee - Prep_Fee) / 2.0
    """
    net_proceeds = (buybox_price * (1.0 - AMAZON_REFERRAL_PCT)) - fba_fee - PREP_FEE_BASELINE
    if net_proceeds <= 0:
        return 0.0
    max_buy_cost = net_proceeds / (1.0 + MIN_ESTIMATED_ROI)
    return round(max_buy_cost, 2)


def match_against_local_distributor_index(brand: str, mpn: str) -> List[Dict]:
    """
    Checks if brand/MPN exists in cached distributor inventories or maps
    to verified wholesale lines (Midland Metal, JB Tools, PND, Global Industrial).
    """
    matches = []
    
    # Static brand-to-distributor distributor matrix for South FL / National lines
    distributor_brand_matrix = {
        "PARKER": ["Midland Metal", "Grainger South FL Hub"],
        "AEROQUIP": ["Midland Metal"],
        "MILWAUKEE": ["JB Tools", "Prime National Distribution"],
        "DEWALT": ["JB Tools", "Prime National Distribution"],
        "PROTO": ["JB Tools", "Global Industrial"],
        "3M": ["Global Industrial", "Prime National Distribution"],
        "LOCTITE": ["Global Industrial", "Grainger South FL Hub"],
        "RUBBERMAID": ["Prime National Distribution", "Global Industrial"]
    }
    
    brand_clean = (brand or "").upper().strip()
    for matrix_brand, distributors in distributor_brand_matrix.items():
        if matrix_brand in brand_clean:
            for dist in distributors:
                matches.append({
                    "distributor": dist,
                    "match_type": "Direct Line Match",
                    "action": f"Request quote for MPN: {mpn}"
                })
                
    return matches


def reverse_source_candidate(candidate_raw: Dict) -> Optional[Dict]:
    """
    Processes a single candidate ASIN, parses part details, and calculates sourcing specs.
    """
    asin = candidate_raw.get("asin")
    title = candidate_raw.get("title", "")
    brand = candidate_raw.get("brand", "")
    mpn = candidate_raw.get("mpn") or candidate_raw.get("model") or ""
    current_price = float(candidate_raw.get("buybox_price") or candidate_raw.get("price", 0.0))
    velocity = int(candidate_raw.get("monthly_velocity", 0))
    fba_sellers = int(candidate_raw.get("active_fba_sellers", 0))

    if current_price <= 0 or velocity < MIN_MONTHLY_VELOCITY:
        return None

    if fba_sellers > MAX_COMPETING_FBA:
        return None

    max_cost_target = calculate_max_target_cost(current_price)
    if max_cost_target <= 1.00:
        return None

    potential_suppliers = match_against_local_distributor_index(brand, mpn)

    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "mpn": mpn,
        "buybox_price": current_price,
        "max_allowable_cost": max_cost_target,
        "target_net_profit": round(max_cost_target, 2), # At 100% ROI, Profit = Cost
        "monthly_velocity": velocity,
        "fba_sellers": fba_sellers,
        "matched_distributors": potential_suppliers,
        "recommended_rfq_action": "Email RFQ" if potential_suppliers else "Brand Distribution Inquiry"
    }


def run_reverse_sourcing_batch(candidates: List[Dict]) -> List[Dict]:
    """
    Batch processor returning qualified reverse RFQ opportunities.
    """
    results = []
    logger.info(f"Analyzing {len(candidates)} ASIN candidates for reverse sourcing...")
    
    for item in candidates:
        parsed = reverse_source_candidate(item)
        if parsed:
            results.append(parsed)

    logger.info(f"Reverse sourcing analysis generated {len(results)} high-margin RFQ targets.")
    
    output_path = OUTPUT_DIR / "daily_reverse_rfqs.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        
    return results
