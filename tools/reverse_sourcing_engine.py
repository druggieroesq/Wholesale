"""
tools/reverse_sourcing_engine.py
Reverse-Sourcing Engine: ASIN -> Vacuum Check -> Brand/MPN -> Landed Logistics -> Wholesaler Matching.
Maps high-demand, zero-supply Amazon ASINs directly to active wholesale catalogs and RFQs.
Enforces 100% Net ROI floor, Layer 0 IP Guard, and dynamic logistics routing.
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

from tools.ip_guard import is_brand_blocked
from tools.logistics_engine import LogisticsEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] ReverseSourcing: %(message)s"
)
logger = logging.getLogger("ReverseSourcing")

DATA_DIR = ROOT_DIR / "data"
CATALOG_DIR = DATA_DIR / "catalogs"
OUTPUT_DIR = DATA_DIR / "reverse_leads"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Sourcing Thresholds
MIN_MONTHLY_VELOCITY = 15          # At least 15 units/month proven run-rate
MAX_COMPETING_FBA = 1              # Strict supply-vacuum parameter (0 or 1 competitor)
MIN_ESTIMATED_ROI = 1.0            # 100% Net ROI floor (Net Profit >= Landed Cost)
AMAZON_REFERRAL_PCT = 0.15

logistics_router = LogisticsEngine()


def estimate_fba_fee(weight_lbs: float) -> float:
    if weight_lbs <= 1.0:
        return 3.86
    elif weight_lbs <= 2.0:
        return 5.40
    elif weight_lbs <= 3.0:
        return 6.10
    return round(6.10 + (weight_lbs - 3.0) * 0.45, 2)


def calculate_max_target_cost(buybox_price: float, weight_lbs: float, fulfillment_overhead: float) -> float:
    """
    Calculates Maximum Allowable Wholesale Cost (MAC) to achieve >= 100% Net ROI:
    Net Profit = BuyBox - (BuyBox * 15%) - FBA_Fee - Total_Overhead (Inbound Freight + Prep) - Cost
    Target: Net Profit >= Total Landed Cost (Cost + Total_Overhead)
    => BuyBox * 0.85 - FBA_Fee - Overhead >= 2 * Cost + 2 * Overhead
    => Max Wholesale Cost = (BuyBox * 0.85 - FBA_Fee - (2 * Overhead)) / 2
    """
    fba_fee = estimate_fba_fee(weight_lbs)
    net_proceeds = (buybox_price * (1.0 - AMAZON_REFERRAL_PCT)) - fba_fee
    
    max_buy_cost = (net_proceeds - (2.0 * fulfillment_overhead)) / 2.0
    return max(0.0, round(max_buy_cost, 2))


def scan_local_catalogs_for_match(brand: str, mpn: str, upc: str) -> List[Dict[str, Any]]:
    """
    Scans ingested distributor JSON files in data/catalogs/ to find real matches
    by MPN, UPC, or brand part numbers.
    """
    matches = []
    if not CATALOG_DIR.exists():
        return matches

    clean_brand = (brand or "").strip().upper()
    clean_mpn = (mpn or "").strip().upper()
    clean_upc = (upc or "").strip().zfill(12)

    for cat_file in CATALOG_DIR.glob("*.json"):
        try:
            with open(cat_file, "r", encoding="utf-8") as f:
                items = json.load(f)
                if not isinstance(items, list):
                    continue

                for item in items:
                    item_mpn = str(item.get("item_number", item.get("mpn", ""))).strip().upper()
                    item_upc = str(item.get("upc", "")).strip().zfill(12)
                    item_brand = str(item.get("brand", "")).strip().upper()

                    # Exact UPC or MPN match
                    if (clean_upc and clean_upc == item_upc) or (clean_mpn and clean_mpn == item_mpn):
                        matches.append({
                            "distributor": item.get("distributor", cat_file.stem),
                            "match_type": "Exact Part/UPC Match",
                            "item_number": item_mpn,
                            "actual_wholesale_cost": float(item.get("wholesale_cost", 0.0)),
                            "stock_available": item.get("stock_qty", "In Stock")
                        })
                    elif clean_brand and clean_brand == item_brand and clean_mpn and clean_mpn in item_mpn:
                        matches.append({
                            "distributor": item.get("distributor", cat_file.stem),
                            "match_type": "Brand Line Match",
                            "item_number": item_mpn,
                            "actual_wholesale_cost": float(item.get("wholesale_cost", 0.0)),
                            "stock_available": item.get("stock_qty", "In Stock")
                        })
        except Exception as e:
            logger.error(f"Error scanning catalog {cat_file.name}: {e}")

    return matches


def reverse_source_candidate(candidate_raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Processes candidate ASIN, parses metrics, verifies IP firewall, routes logistics,
    and returns high-probability RFQ targets.
    """
    asin = candidate_raw.get("asin")
    title = candidate_raw.get("title", "")
    brand = candidate_raw.get("brand", "")
    mpn = candidate_raw.get("mpn") or candidate_raw.get("model") or ""
    upc = candidate_raw.get("upc", "")
    current_price = float(candidate_raw.get("buybox_price") or candidate_raw.get("price", 0.0))
    velocity = int(candidate_raw.get("monthly_velocity", 0))
    fba_sellers = int(candidate_raw.get("active_fba_sellers", candidate_raw.get("fba_sellers", 0)))
    weight_lbs = float(candidate_raw.get("package_weight_lbs", candidate_raw.get("weight_lbs", 1.5)))

    # 1. Layer 0 IP Guard Check (Drops 659+ protected brands immediately)
    blocked, reason = is_brand_blocked(brand=brand, title=title)
    if blocked:
        logger.debug(f"IP Blocked: {brand} ({reason})")
        return None

    # 2. Demand Velocity & Supply-Vacuum Gate
    if current_price < 25.0 or velocity < MIN_MONTHLY_VELOCITY:
        return None

    if fba_sellers > MAX_COMPETING_FBA:
        return None

    # 3. Dynamic Logistics Routing (Evaluates Will-Call, Freight, and 3PL options)
    # Check default routing based on target profile
    route_info = logistics_router.route_purchase_order(
        vendor_key="will_call_hub" if "FL" in candidate_raw.get("region", "") else "standard_supplier",
        unit_cost=current_price * 0.35,  # Estimated baseline cost
        weight_lbs=weight_lbs
    )
    total_fulfillment_overhead = route_info["total_fulfillment_overhead"]

    # 4. Maximum Allowable Cost calculation for 100% Net ROI
    max_cost_target = calculate_max_target_cost(current_price, weight_lbs, total_fulfillment_overhead)
    if max_cost_target <= 2.00:
        return None

    # 5. Check Ingested Catalogs for Existing Wholesale Lines
    matched_catalog_items = scan_local_catalogs_for_match(brand, mpn, upc)

    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "mpn": mpn,
        "upc": upc,
        "buybox_price": current_price,
        "max_allowable_cost": max_cost_target,
        "fba_sellers": fba_sellers,
        "monthly_velocity": velocity,
        "weight_lbs": weight_lbs,
        "assigned_logistics_hub": route_info["assigned_node_name"],
        "fulfillment_overhead_unit": total_fulfillment_overhead,
        "matched_catalogs": matched_catalog_items,
        "recommended_action": "Issue Direct PO" if matched_catalog_items else "Submit Distributor RFQ"
    }


def run_reverse_sourcing_batch(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    logger.info(f"Analyzing {len(candidates)} ASINs through reverse-sourcing engine...")
    
    for item in candidates:
        parsed = reverse_source_candidate(item)
        if parsed:
            results.append(parsed)

    logger.info(f"Reverse sourcing produced {len(results)} validated targets (100% Net ROI floor).")
    
    output_path = OUTPUT_DIR / "daily_reverse_rfqs.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        
    return results


if __name__ == "__main__":
    test_asins = [
        {
            "asin": "B08TEST001",
            "title": "Commercial Brass Ball Valve 3/4 Inch High Pressure",
            "brand": "Apollo Flow",
            "mpn": "70-104-01",
            "buybox_price": 54.00,
            "monthly_velocity": 32,
            "active_fba_sellers": 0,
            "package_weight_lbs": 2.1
        },
        {
            "asin": "B08BLOCKED",
            "title": "Heavy Duty Industrial Safety Glasses Clear",
            "brand": "3M",  # Must be blocked by IP Guard
            "mpn": "11329-00000-20",
            "buybox_price": 45.00,
            "monthly_velocity": 120,
            "active_fba_sellers": 1,
            "package_weight_lbs": 0.5
        },
        {
            "asin": "B08CROWDED",
            "title": "Industrial Wire Stripper Multi-Tool",
            "brand": "Generic Tooling",
            "mpn": "STRIP-101",
            "buybox_price": 38.00,
            "monthly_velocity": 45,
            "active_fba_sellers": 6,  # Must be blocked by Vacuum gate (>1 seller)
            "package_weight_lbs": 1.0
        }
    ]
    
    leads = run_reverse_sourcing_batch(test_asins)
    print(f"\nQualified Reverse Targets: {len(leads)}")
    for l in leads:
        print(f"ASIN: {l['asin']} | Brand: {l['brand']} | Target Wholesale: <= ${l['max_allowable_cost']} | Action: {l['recommended_action']}")
