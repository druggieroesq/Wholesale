#!/usr/bin/env python3
"""
MRO Central Logistics, Fee Engine & Portal Resolver (v5.5 Production Master)
----------------------------------------------------------------------------
- Central fee benchmarks: Parkland 3PL Prep ($0.35 single / $0.45 bundle)
- Regional Inbound Freight & South Florida Will-Call ($0.00) routing
- Dynamic FBA fee tiers and Amazon referral fee modeling (15%)
- Autonomous 50+ distributor fallback & dynamic search portal resolution
- Incentive Optimizer: Free Freight Allowance (FFA), cash terms, tiered rebates
- Multipack & Bundle Guard: Regex pack count extraction
- Dynamic Tiered Profit Hurdle:
    * >= 75% ROI base + >= $10.00 net profit floor
    * >= 58% ROI if monthly sales velocity > 500 units/mo (retaining $10.00 floor)
- Universal Product Safety & Risk Gate:
    * Hazmat & adult product flags
    * Meltable / heat-sensitive seasonal goods
    * Amazon 1P Buy Box dominance (> 15% 90-day share)
    * Brand IP takedown sweeps (seller count collapse)
    * 180-day smoothed velocity floors (min 50 units/mo)
    * Review sentiment floors (< 3.2 stars)
"""

import re
import math
import urllib.parse
from typing import Dict, Any, Optional, Tuple

# ==========================================
# 1. LOGISTICS BENCHMARKS & PREP OVERHEAD
# ==========================================
PARKLAND_PREP_BASE = 0.35        # Base single-unit FBA prep cost ($0.35/ea)
MULTIPACK_PREP_ADDON = 0.10      # Bundling / poly-bag / suffocation label add-on ($0.45 total)
AMAZON_REFERRAL_RATE = 0.15      # Standard Amazon referral commission (15%)

# Vendors eligible for direct local hub pickup ($0.00 freight / expedited turnaround)
LOCAL_WILL_CALL_VENDORS = {
    "bci", "bci_wholesale", "contour", "contour_wholesale", 
    "florida_commercial", "atlantic_spice"
}

# Master distributor business rules and portal search architectures
DISTRIBUTOR_RULES = {
    "frontier": {
        "name": "Frontier Co-op Wholesale",
        "free_freight_minimum": 250.0,
        "base_discount": 0.0,
        "search_url_template": "https://wholesale.frontiercoop.com/search?q={query}",
        "payment_terms": {
            "payment_method": "CREDIT_CARD",
            "cc_surcharge_pct": 0.0,
            "early_pay_discount_pct": 0.0,
            "settlement_window_days": 30
        }
    },
    "contour": {
        "name": "Contour Abrasives & MRO Wholesale",
        "free_freight_minimum": 1500.0,
        "base_discount": 0.0,
        "search_url_template": "https://www.contourabrasives.com/search?keywords={query}",
        "payment_terms": {
            "payment_method": "CREDIT_CARD",
            "cc_surcharge_pct": 0.0,
            "early_pay_discount_pct": 0.02,
            "settlement_window_days": 30
        }
    },
    "bci": {
        "name": "BCI Industrial Fasteners & Supply",
        "free_freight_minimum": 0.0,  # Local will-call pickup in Parkland / South FL
        "base_discount": 0.0,
        "search_url_template": "https://www.bci-fasteners.com/search?query={query}",
        "payment_terms": {
            "payment_method": "ACH",
            "cc_surcharge_pct": 0.0,
            "early_pay_discount_pct": 0.0,
            "settlement_window_days": 30
        }
    },
    "midland": {
        "name": "Midland Metal Wholesale",
        "free_freight_minimum": 2000.0,
        "base_discount": 0.0,
        "search_url_template": "https://www.midlandmetal.com/search?searchKeyword={query}",
        "payment_terms": {
            "payment_method": "TERMS_2_10_NET_30",
            "cc_surcharge_pct": 0.0,
            "early_pay_discount_pct": 0.02,
            "settlement_window_days": 30
        }
    },
    "zoro": {
        "name": "Zoro Industrial Direct",
        "free_freight_minimum": 50.0,
        "base_discount": 0.0,
        "search_url_template": "https://www.zoro.com/search?q={query}",
        "payment_terms": {
            "payment_method": "CREDIT_CARD",
            "cc_surcharge_pct": 0.0,
            "early_pay_discount_pct": 0.0,
            "settlement_window_days": 30
        }
    }
}


# ==========================================
# 2. FREIGHT & FBA FEE ENGINES
# ==========================================
def calculate_inbound_freight(vendor_key: str, weight_lbs: float, total_cogs: float = 0.0) -> float:
    """
    Calculates inbound transit cost per unit/bundle into Parkland, FL fulfillment hub.
    - Zero cost ($0.00) for South Florida will-call suppliers.
    - Zero cost ($0.00) if order value unlocks distributor Free Freight Allowance (FFA).
    - Tiered freight curve: $0.45/lb base, tapering down for heavier commercial shipments.
    """
    clean_vendor = str(vendor_key or "").strip().lower()

    # 1. Local will-call self-pickup
    if any(k in clean_vendor for k in LOCAL_WILL_CALL_VENDORS):
        return 0.0

    # 2. Vendor Free Freight Allowance (FFA)
    dist_info = DISTRIBUTOR_RULES.get(clean_vendor)
    if dist_info and total_cogs >= dist_info.get("free_freight_minimum", 99999.0):
        return 0.0

    # 3. Regional freight tiering
    safe_weight = max(0.2, float(weight_lbs or 0.0))
    if safe_weight <= 1.0:
        return 0.45
    elif safe_weight <= 5.0:
        return round(0.45 + ((safe_weight - 1.0) * 0.35), 2)
    elif safe_weight <= 20.0:
        return round(1.85 + ((safe_weight - 5.0) * 0.28), 2)
    else:
        return round(6.05 + ((safe_weight - 20.0) * 0.22), 2)


def estimate_fba_fee(weight_lbs: float) -> float:
    """Calculates dynamic Amazon FBA fulfillment fees based on shipping weight."""
    w = max(0.1, float(weight_lbs or 0.0))
    if w <= 0.25:
        return 3.22
    elif w <= 0.50:
        return 3.40
    elif w <= 0.75:
        return 3.58
    elif w <= 1.0:
        return 3.77
    elif w <= 1.5:
        return 4.52
    elif w <= 2.0:
        return 5.14
    elif w <= 3.0:
        return 5.79
    else:
        extra_units = math.ceil((w - 3.0) / 0.5)
        return round(5.79 + (extra_units * 0.38), 2)


# ==========================================
# 3. MULTIPACK & DYNAMIC HURDLE ENGINE
# ==========================================
def extract_pack_count(title: str, default: int = 1) -> int:
    """Extracts pack count from listing title (e.g. 'Pack of 4', '4-Pack', '4PK', 'Case of 12')."""
    if not title:
        return default
    
    clean_title = title.lower()
    patterns = [
        r'(?:pack of|box of|case of|set of)\s*(\d+)',
        r'(\d+)\s*(?:pack|pk|count|ct)\b',
        r'\((\d+)\s*pack\)',
        r'\b(\d+)-pack\b'
    ]
    
    for pat in patterns:
        match = re.search(pat, clean_title)
        if match:
            try:
                count = int(match.group(1))
                if 1 <= count <= 200:
                    return count
            except (ValueError, TypeError):
                continue
    return default


def calculate_dynamic_hurdles(
    buy_box_price: float,
    weight_lbs: float = 1.0,
    vendor_key: str = "",
    pack_count: int = 1,
    monthly_velocity: int = 0
) -> Dict[str, Any]:
    """
    Calculates Net Payout and Maximum Allowable Cost (MAC) against dynamic hurdle:
      - 58% ROI if monthly velocity > 500 units/mo
      - 75% ROI base otherwise
      - Minimum $10.00 net profit floor across all tiers
    """
    if buy_box_price <= 0:
        return {
            "net_payout": 0.0,
            "target_roi": 0.75,
            "target_mac": 0.0,
            "min_profit_floor": 10.00
        }

    referral_fee = round(buy_box_price * AMAZON_REFERRAL_RATE, 2)
    fba_pick_pack = estimate_fba_fee(weight_lbs)
    inbound_freight = calculate_inbound_freight(vendor_key, weight_lbs)
    prep_fee = PARKLAND_PREP_BASE + (MULTIPACK_PREP_ADDON if pack_count > 1 else 0.0)

    net_payout = round(buy_box_price - referral_fee - fba_pick_pack - inbound_freight - prep_fee, 2)

    target_roi = 0.58 if monthly_velocity > 500 else 0.75
    min_profit_floor = 10.00

    mac_by_roi = net_payout / (1.0 + target_roi)
    mac_by_profit = net_payout - min_profit_floor
    target_mac = max(0.0, min(mac_by_roi, mac_by_profit))

    return {
        "buy_box_price": round(buy_box_price, 2),
        "referral_fee": referral_fee,
        "fba_pick_pack": fba_pick_pack,
        "inbound_freight": inbound_freight,
        "prep_fee": round(prep_fee, 2),
        "net_payout": net_payout,
        "target_roi": target_roi,
        "target_mac": round(target_mac, 2),
        "min_profit_floor": min_profit_floor
    }


def evaluate_deal_profitability(
    supplier_unit_cost: float,
    buy_box_price: float,
    weight_lbs: float = 1.0,
    vendor_key: str = "",
    pack_count: int = 1,
    monthly_velocity: int = 0
) -> Tuple[bool, Dict[str, Any]]:
    """Verifies if a supplier cost satisfies the dynamic hurdle and $10 profit floor."""
    total_cogs = round(supplier_unit_cost * pack_count, 2)
    hurdles = calculate_dynamic_hurdles(
        buy_box_price=buy_box_price,
        weight_lbs=weight_lbs,
        vendor_key=vendor_key,
        pack_count=pack_count,
        monthly_velocity=monthly_velocity
    )
    
    net_payout = hurdles["net_payout"]
    target_roi = hurdles["target_roi"]

    net_profit = round(net_payout - total_cogs, 2)
    actual_roi = round(net_profit / total_cogs, 4) if total_cogs > 0 else 0.0

    passes = (net_profit >= 10.00) and (actual_roi >= target_roi)

    metrics = {
        "pack_count": pack_count,
        "total_cogs": total_cogs,
        "net_profit": net_profit,
        "actual_roi": actual_roi,
        "target_roi": target_roi,
        "passes_hurdle": passes,
        **hurdles
    }

    return passes, metrics


# ==========================================
# 4. SUPPLIER INCENTIVE & ORDER PERKS OPTIMIZER
# ==========================================
def calculate_order_perks(distributor_meta: Dict[str, Any], gross_spend: float) -> Dict[str, Any]:
    """Evaluates supplier incentives, Free Freight Allowances (FFA), and volume discount tiers."""
    distributor_meta = distributor_meta or {}
    ffa_threshold = float(distributor_meta.get("free_freight_minimum", 2000.0))
    qualifies_ffa = gross_spend >= ffa_threshold

    discount_pct = 0.0
    if gross_spend >= 5000.0:
        discount_pct = 0.03
    elif gross_spend >= 2500.0:
        discount_pct = 0.015

    early_pay = float(distributor_meta.get("payment_terms", {}).get("early_pay_discount_pct", 0.0))

    return {
        "gross_spend": round(gross_spend, 2),
        "free_freight_unlocked": qualifies_ffa,
        "volume_discount_pct": discount_pct,
        "early_pay_discount_pct": early_pay,
        "total_rebate_pct": round(discount_pct + early_pay, 4)
    }


# ==========================================
# 5. PRODUCT SAFETY & 180-DAY VELOCITY SMOOTHING
# ==========================================
MELTABLE_KEYWORDS = {
    "chocolate", "gummy", "gummies", "wax", "candle", 
    "melt", "softgel", "marshmallow"
}

def get_monthly_sales_drops(keepa_product: Dict[str, Any]) -> int:
    """
    Estimates monthly unit velocity from 90d/180d sales rank smoothing.
    Protects OOS replenishment items from false-negative drops.
    """
    stats = keepa_product.get("stats") or {}
    drops_30 = stats.get("salesRankDrops30", 0)
    drops_90 = stats.get("salesRankDrops90", 0)

    if drops_30 and drops_30 > 0:
        return int(drops_30)
    if drops_90 and drops_90 > 0:
        return max(1, int(round(drops_90 / 3.0)))

    # Fallback to 180-day smoothed rank curve
    curr = stats.get("current") or []
    c_bsr = curr[3] if len(curr) > 3 and curr[3] and curr[3] > 0 else None
    a180 = stats.get("avg180") or []
    avg_180 = a180[3] if len(a180) > 3 and a180[3] and a180[3] > 0 else None
    eff_bsr = min([r for r in [c_bsr, avg_180] if r and r > 0] or [999999])

    if eff_bsr <= 1000:
        return 500
    elif eff_bsr <= 5000:
        return 200
    elif eff_bsr <= 25000:
        return 80
    elif eff_bsr <= 50000:
        return 40
    elif eff_bsr <= 100000:
        return 15
    return 5


def audit_product_safety(
    keepa_product: Dict[str, Any],
    max_amz_share: float = 0.15,
    min_velocity_floor: int = 50
) -> Tuple[bool, str]:
    """Audits listings for zero-tolerance risk factors and 50 units/mo velocity floor."""
    if not keepa_product or not isinstance(keepa_product, dict):
        return False, "Invalid product payload"

    stats = keepa_product.get("stats") or {}

    # 1. Hazmat & Dangerous Goods
    if keepa_product.get("isDangerousGoods", False) or keepa_product.get("isHazmat", False):
        return False, "Hazmat: Hazardous materials certification required."

    # 2. Adult Product
    if keepa_product.get("isAdultProduct", False):
        return False, "Restricted: Adult category product."

    # 3. Meltable Goods
    title = (keepa_product.get("title") or "").lower()
    if any(w in title for w in MELTABLE_KEYWORDS):
        return False, "Meltable: Temperature-sensitive product prohibited during warm seasons."

    # 4. Amazon 1P Buy Box Share
    amz_oos_90 = stats.get("outOfStockPercentage90", 0)
    amz_oos_rate = 0
    if isinstance(amz_oos_90, list) and len(amz_oos_90) > 0:
        amz_oos_rate = amz_oos_90[0]
    elif isinstance(amz_oos_90, (int, float)):
        amz_oos_rate = amz_oos_90

    if amz_oos_rate > 0 and amz_oos_rate < (100 - (max_amz_share * 100)):
        return False, f"Amazon 1P Dominance: Amazon holds Buy Box > {max_amz_share * 100:.0f}% of past 90 days."

    # 5. IP Lawsuit / Sudden Seller Count Collapse
    live_sellers = keepa_product.get("liveSellers") or []
    current_fba = stats.get("current_fba_sellers", 0)
    if isinstance(live_sellers, list) and len(live_sellers) >= 2:
        peak_sellers = max(live_sellers)
        if peak_sellers >= 5 and current_fba <= 1:
            return False, f"Suspected Brand IP Sweep: Sellers collapsed from {peak_sellers} to {current_fba}."

    # 6. Defensive Velocity Floor (>= 50 units/month)
    monthly_velocity = get_monthly_sales_drops(keepa_product)
    if monthly_velocity < min_velocity_floor:
        return False, f"Insufficient sales velocity ({monthly_velocity} units/mo, min {min_velocity_floor} required)."

    # 7. Customer Sentiment & Review Floor
    total_reviews = stats.get("reviewCount", 0)
    rating = stats.get("avgRating", 0)
    if rating > 5:
        rating = rating / 10.0

    if total_reviews >= 10 and 0 < rating < 3.2:
        return False, f"Poor customer sentiment: {rating:.1f} stars on {total_reviews} reviews."

    return True, "PASSED"


# ==========================================
# 6. 50+ DISTRIBUTOR DEEP-LINK RESOLVER
# ==========================================
def resolve_order_url(distributor_key: str, mpn: str = "", title: str = "", asin: str = "") -> Optional[str]:
    """
    Constructs a direct portal search deep-link for distributor ordering.
    Automatically formats query for known distributors or generates clean vendor query.
    """
    clean_dist = str(distributor_key or "").strip().lower()
    dist_info = DISTRIBUTOR_RULES.get(clean_dist)

    search_query = mpn if mpn and mpn != "N/A" else ""
    if not search_query and title:
        tokens = [t for t in title.split() if len(t) >= 3 and t.isalnum()][:4]
        search_query = " ".join(tokens)

    if not search_query and asin:
        search_query = asin

    if dist_info and "search_url_template" in dist_info:
        encoded_q = urllib.parse.quote(search_query)
        return dist_info["search_url_template"].format(query=encoded_q)

    # Clean fallback for any of the 50+ new distributors
    if search_query:
        encoded_q = urllib.parse.quote(f"{clean_dist} {search_query}".strip())
        return f"https://www.google.com/search?q={encoded_q}"

    return None
