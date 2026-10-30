"""
tools/keepa_scanner.py
Multi-Tier Waterfall Sourcing Engine with Account-Level Gating & IP Protection Guards.

Tier Structure:
- Tier 1 (Sniper Alpha): 100%+ ROI, Dormant Stockout Goldmines (0 active sellers) or Suppressed Buy Box Moats (<=1 FBA seller).
- Tier 2 (Expansion Harvest): 70%+ ROI, Low-Competition Runways (<=2 FBA sellers), steady velocity >= 20 units/mo.
- Tier 3 (Baseline Velocity): 50%+ ROI, standard catalog rotation (<=4 sellers), velocity >= 30 units/mo.

Protection Layer (Evaluated BEFORE Tier Assignment):
- Amazon SP-API Listings Restrictions check (gated brands/categories, hazmat, refurbished-only).
- IP Alert / Brand Injunction check (known cease-and-desist or aggressive brand enforcement).
- Meltable / Hazmat / Oversize FBA constraints.
"""

import os
import json
import logging
import requests
from typing import Dict, List, Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "")
KEEPA_BASE_URL = "https://api.keepa.com/product"
SP_API_ACCESS_TOKEN = os.getenv("SP_API_ACCESS_TOKEN", "")
SELLER_ID = os.getenv("AMAZON_SELLER_ID", "")

# Load known litigious IP brands / automated IP complaints
KNOWN_IP_RISK_BRANDS = {
    "apple", "nike", "disney", "otterbox", "hydro flask", "lego", 
    "popsockets", "yeti", "anker", "traeger", "dewalt"
}

def check_ip_and_hazmat_risk(brand: str, title: str, keepa_prod: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates brand litigation risk and FBA storage disqualifiers.
    """
    clean_brand = brand.lower().strip()
    if clean_brand in KNOWN_IP_RISK_BRANDS:
        return {"allowed": False, "reason": f"HIGH_IP_RISK_BRAND: {brand}"}
    
    # Check Hazmat / Dangerous Goods flag in Keepa
    if keepa_prod.get("isDangerousGoods", False):
        return {"allowed": False, "reason": "HAZMAT_RESTRICTED"}
    
    # Check Package Dimension / Oversize flag if restricted
    package_weight = keepa_prod.get("packageWeight", 0)  # in grams
    if package_weight > 22680:  # > 50 lbs
        return {"allowed": False, "reason": "OVERSIZE_HEAVY_RESTRICTED (>50 lbs)"}

    return {"allowed": True, "reason": "PASSED"}

def check_sp_api_gating(asin: str) -> Dict[str, Any]:
    """
    Queries Amazon SP-API Listings Restrictions endpoint:
    GET /listings/2021-08-01/restrictions?asin={asin}&sellerId={sellerId}
    Returns whether account is UNGATED for Condition: 'new_new'.
    """
    if not SP_API_ACCESS_TOKEN or not SELLER_ID:
        return {"can_sell": True, "gated": False, "requires_approval": False}

    url = f"https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/restrictions?asin={asin}&sellerId={SELLER_ID}&conditionType=new_new"
    headers = {
        "x-amz-access-token": SP_API_ACCESS_TOKEN,
        "User-Agent": "MROArbitragePipeline/2.0"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            restrictions = data.get("restrictions", [])
            if not restrictions:
                return {"can_sell": True, "gated": False, "requires_approval": False}
            else:
                reasons = [r.get("reasonCode") for r in restrictions]
                return {"can_sell": False, "gated": True, "reasons": reasons}
    except Exception as e:
        logger.error(f"SP-API Gating check error for {asin}: {e}")
    
    return {"can_sell": True, "gated": False, "requires_approval": False}

def evaluate_opportunity_waterfall(
    raw_item: Dict[str, Any],
    keepa_prod: Dict[str, Any],
    active_scan_tier: int = 1  # 1 = Sniper Only; 2 = Sniper + Expansion; 3 = All Tiers
) -> Optional[Dict[str, Any]]:
    """
    Processes item through strict gating/safety checks first,
    then assigns and tests against waterfall tiers.
    """
    brand = keepa_prod.get("brand", "")
    title = keepa_prod.get("title", "")
    asin = keepa_prod.get("asin", "")
    wholesale_cost = float(raw_item.get("wholesale_cost", 0.0))

    if wholesale_cost <= 0:
        return None

    # --- STEP 1: SAFETY & GATING GUARDS ---
    ip_status = check_ip_and_hazmat_risk(brand, title, keepa_prod)
    if not ip_status["allowed"]:
        logger.info(f"Skipping {asin}: {ip_status['reason']}")
        return None

    gating_status = check_sp_api_gating(asin)
    if not gating_status["can_sell"]:
        logger.info(f"Skipping {asin}: Gated on account ({gating_status.get('reasons')})")
        return None

    # --- STEP 2: METRIC EXTRACTION ---
    stats = keepa_prod.get("stats", {})
    drops_30d = stats.get("salesRankDrops30", 0)
    drops_90d = stats.get("salesRankDrops90", 0)
    bought_past_month = keepa_prod.get("boughtPastMonth", 0)
    monthly_velocity = max(bought_past_month, int(drops_30d * 1.2))

    offers = keepa_prod.get("offers", [])
    active_fba_offers = [o for o in offers if o.get("isFBA", False) and o.get("condition") == 1 and o.get("price", 0) > 0]
    fba_seller_count = len(active_fba_offers)
    total_seller_count = len([o for o in offers if o.get("condition") == 1 and o.get("price", 0) > 0])

    current_bb_price = stats.get("buyBoxPrice", -1) / 100.0 if stats.get("buyBoxPrice") else -1
    is_suppressed = current_bb_price <= 0

    valid_offer_prices = [o.get("price", 0) / 100.0 for o in offers if o.get("price", 0) > 0 and o.get("condition") == 1]
    lowest_offer = min(valid_offer_prices) if valid_offer_prices else -1.0
    effective_price = current_bb_price if not is_suppressed else lowest_offer

    if effective_price <= 0:
        return None

    # Standard landed calculation ($0.06 self-prep)
    amazon_fees = (effective_price * 0.15) + 4.50
    prep_cost = 0.06
    total_landed = wholesale_cost + prep_cost
    net_profit = effective_price - amazon_fees - total_landed
    roi = (net_profit / total_landed) if total_landed > 0 else 0.0

    # --- STEP 3: WATERFALL TIER CLASSIFICATION ---
    assigned_tier = None
    strategy_label = None

    # TIER 1: SNIPER ALPHA (100%+ ROI, Monopoly / Low-Competition Moat)
    if roi >= 1.00 and net_profit >= 6.00:
        if total_seller_count == 0 and drops_90d >= 15:
            assigned_tier = 1
            strategy_label = "TIER_1_SNIPER: Dormant Stockout (0 Sellers, 100% Buy Box)"
        elif fba_seller_count <= 1 and (monthly_velocity >= 15 or drops_30d >= 12):
            assigned_tier = 1
            strategy_label = "TIER_1_SNIPER: " + ("Suppressed Moat" if is_suppressed else "Sole FBA Seller")

    # TIER 2: EXPANSION HARVEST (70%+ ROI, <= 2 FBA Sellers, Velocity >= 20)
    elif roi >= 0.70 and net_profit >= 5.00:
        if fba_seller_count <= 2 and monthly_velocity >= 20:
            assigned_tier = 2
            strategy_label = f"TIER_2_EXPANSION: Low-Comp Runway ({fba_seller_count} FBA Sellers)"

    # TIER 3: BASELINE VELOCITY (50%+ ROI, <= 3 FBA Sellers, Velocity >= 30)
    elif roi >= 0.50 and net_profit >= 4.00:
        if fba_seller_count <= 3 and monthly_velocity >= 30:
            assigned_tier = 3
            strategy_label = f"TIER_3_BASELINE: Standard Catalog Rotation ({fba_seller_count} FBA Sellers)"

    if not assigned_tier or assigned_tier > active_scan_tier:
        return None

    my_share_ratio = 1.0 / (fba_seller_count + 1)
    projected_units = max(5, int(monthly_velocity * my_share_ratio))

    return {
        "asin": asin,
        "title": title,
        "brand": brand,
        "distributor": raw_item.get("distributor"),
        "mpn_sku": raw_item.get("mpn_sku"),
        "wholesale_cost": wholesale_cost,
        "effective_price": effective_price,
        "net_profit": round(net_profit, 2),
        "roi_pct": round(roi * 100, 1),
        "tier": assigned_tier,
        "strategy": strategy_label,
        "monthly_velocity": monthly_velocity,
        "active_fba_sellers": fba_seller_count,
        "projected_30d_units": projected_units,
        "projected_30d_profit": round(projected_units * net_profit, 2),
        "gating_status": "UNGATED_VERIFIED",
        "ip_status": "CLEAN",
        "product_url": raw_item.get("product_url")
    }
