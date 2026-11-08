#!/usr/bin/env python3
"""
config.py
---------
Master Wholesale MRO & Beauty Sourcing Configuration (v17.0 Proximity Sourcing Master).
Enforces:
1. Strict Distributor-to-FC Proximity Routing (`get_logistics_profile`):
   * BCI -> Makki Prep (Harrisburg, PA) [Northeast Corridor - MDT1/PHL7]
   * Marlo Beauty & JB Tools -> Detroit Area 3PL Partner (Southeast MI) [Midwest - DET1/DET2]
   * Grainger & Ferguson -> South FL Local Hub (Fort Lauderdale / Pompano Beach, FL) [South FL - MIA1/FLL1]
   * Unmatched / Target Acquisitions -> Direct Manufacturer / Local Hub (Proximity Matching Required)
2. Continuous Velocity Engine (`calculate_continuous_velocity`):
   Blends graph activity (Keepa drops) with power-law BSR curves to prevent flat plateaus.
3. Dynamic Velocity-Tiered Profit Hurdle (`get_dynamic_profit_hurdle`):
   * High Velocity (>= 100 units/mo): $350.00/mo minimum net profit pool.
   * Mid Velocity (50-99 units/mo): $500.00/mo minimum net profit pool.
   * Low Velocity (< 50 units/mo): $750.00/mo minimum net profit pool.
4. Rotational Velocity Allocation (`validate_fba_competition`):
   * 0 Competitors: 100% Buy Box share.
   * 1 Competitor: 50% Rotational split (Velocity / 2).
   * 2 to 5 Competitors: Velocity / (comp + 1).
   * Max 5 FBA sellers allowed.
5. Turnaround Ceiling:
   * Hard Ceiling: MAX_DAYS_TO_FBA = 20 days.
"""

import os
import math
from typing import Tuple, Dict, Any

FALLBACK_WEBHOOK_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwwRsM3DWfYtE0D_Tr6cmayvo-twV8gXMLo0MzT8bvqbqsIRFbWIJpoQRW7TzDcxR9Y/exec"
)

def get_verified_webhook_url() -> str:
    url = (
        os.getenv("BUY_LIST_WEBHOOK_URL")
        or os.getenv("GOOGLE_SHEET_WEBHOOK_URL")
        or ""
    ).strip().strip('"').strip("'")
    if not url or "/library/" in url or "PASTE_YOUR" in url:
        return FALLBACK_WEBHOOK_URL
    return url


# CORE GLOBAL HURDLES & LOGISTICS LIMITS
MIN_MONTHLY_PROFIT_POOL = 750.00
MAX_DAYS_TO_FBA = 20
MAX_COMPETITIVE_FBA = 5

LOGISTICS_PROFILES: Dict[str, Dict[str, Any]] = {
    # 1. BCI: Pennsylvania Distributor strictly routed to Harrisburg Prep
    "BCI": {
        "facility": "Makki Prep (Harrisburg, PA)",
        "prep_base": 0.80,
        "freight_per_lb": 0.25,
        "landed_estimate": 1.54,
        "lead_time_days": 3,
        "supports_hazmat": True,
    },
    # 2. Michigan Distributors strictly routed to Detroit 3PL Partner
    "MARLO": {
        "facility": "Detroit Area 3PL Partner (Southeast MI)",
        "prep_base": 0.65,
        "freight_per_lb": 0.12,
        "landed_estimate": 1.10,
        "lead_time_days": 2,
        "supports_hazmat": True,
    },
    "JB_TOOLS": {
        "facility": "Detroit Area 3PL Partner (Southeast MI)",
        "prep_base": 0.65,
        "freight_per_lb": 0.12,
        "landed_estimate": 1.10,
        "lead_time_days": 2,
        "supports_hazmat": True,
    },
    # 3. South Florida Trade Counters (Grainger / Ferguson Will-Call)
    "SOUTH_FLORIDA": {
        "facility": "South FL Local Hub (Fort Lauderdale / Pompano Beach, FL)",
        "prep_base": 0.50,
        "freight_per_lb": 0.10,
        "landed_estimate": 0.95,
        "lead_time_days": 1,
        "supports_hazmat": True,
    },
    # 4. Fallback for unmapped brands: Never default to Makki Prep
    "PROXIMITY_REQUIRED": {
        "facility": "Local Hub (Proximity Matching Required)",
        "prep_base": 0.75,
        "freight_per_lb": 0.20,
        "landed_estimate": 1.35,
        "lead_time_days": 3,
        "supports_hazmat": True,
    },
}


def get_logistics_profile(vendor_name: str) -> Dict[str, Any]:
    """
    Routes distributors strictly based on regional proximity to Amazon fulfillment nodes.
    Makki Prep is exclusively assigned to BCI.
    """
    v = (vendor_name or "").upper().strip()

    # BCI is the only vendor authorized for Makki Prep
    if "BCI" in v:
        return LOGISTICS_PROFILES["BCI"]

    # Michigan regional distributors
    elif "MARLO" in v:
        return LOGISTICS_PROFILES["MARLO"]
    elif "JB" in v or "TOOLS" in v:
        return LOGISTICS_PROFILES["JB_TOOLS"]

    # South Florida local will-call hubs
    elif any(hub_kw in v for hub_kw in ("GRAINGER", "FERGUSON", "SOUTH FL", "FT LAUDERDALE", "POMPANO")):
        return LOGISTICS_PROFILES["SOUTH_FLORIDA"]

    # All target brands and unverified vendors require geographic matching
    return LOGISTICS_PROFILES["PROXIMITY_REQUIRED"]


def estimate_days_to_fba(vendor_name: str, is_hazmat: bool = False, pack_qty: int = 1) -> int:
    """
    Calculates total turnaround days from order placement to live Amazon FBA:
    - Vendor dispatch to regional 3PL / will-call: 1-3 days
    - Prep, poly-bagging, and labeling: 1-2 days
    - Amazon FC receiving and check-in: 4 days
    - Strict ceiling applied: <= 20 days
    """
    profile = get_logistics_profile(vendor_name)
    base_lead = profile.get("lead_time_days", 3)
    prep_days = 2 if pack_qty > 1 else 1
    if is_hazmat:
        prep_days += 1
    dock_days = 4
    total = int(base_lead + prep_days + dock_days)
    return min(total, MAX_DAYS_TO_FBA)


def calculate_continuous_velocity(sales_rank: int, drops_30: int = 0, drops_90: int = 0, monthly_sold: int = 0) -> int:
    """
    Computes natural, realistic sales velocity using continuous power-law modeling
    blended with BSR drop frequency. Avoids discrete plateau steps (e.g., flat 1200).
    """
    if monthly_sold and monthly_sold >= 30 and monthly_sold != 1200:
        return int(monthly_sold)

    d30 = max(0, int(drops_30 or 0))
    d90 = max(0, int(drops_90 or 0))
    rank = max(1, int(sales_rank or 0))

    if d30 > 0:
        activity_units = d30 * 2.8
    elif d90 > 0:
        activity_units = (d90 / 3.0) * 2.8
    else:
        activity_units = 15.0

    rank_est = 65000.0 * math.pow(rank, -0.58)

    # Subcategory check: attenuate rank if 30-day drop activity is low
    if rank < 1000 and d30 < 35:
        rank_est = min(rank_est, activity_units * 1.5)

    blended = (activity_units * 0.55) + (rank_est * 0.45)
    return max(30, int(round(blended)))


def get_dynamic_profit_hurdle(total_velocity: float) -> float:
    """
    Tiered minimum net profit pool:
    - >= 100 units/mo: $350.00/mo minimum net profit pool
    - 50-99 units/mo: $500.00/mo minimum net profit pool
    - < 50 units/mo: $750.00/mo minimum net profit pool
    """
    v = float(total_velocity or 0.0)
    if v >= 100.0:
        return 350.00
    elif v >= 50.0:
        return 500.00
    return 750.00


def calculate_sliding_profit_floor(velocity: float) -> Tuple[float, float]:
    """
    Dynamic profit and ROI hurdles:
    - Velocity <= 50 units/mo: $8.00 floor, 55.0% min ROI
    - Velocity >= 500 units/mo: $4.50 floor, 40.0% min ROI
    """
    v = float(velocity or 0.0)
    if v <= 50.0:
        return 8.00, 55.0
    elif v >= 500.0:
        return 4.50, 40.0

    steps = int((v - 50.0) // 50.0)
    profit_floor = max(4.50, 8.00 - (steps * 0.40))

    if v <= 300.0:
        min_roi = 55.0
    else:
        min_roi = max(40.0, 55.0 - ((v - 300.0) / 200.0) * 15.0)

    return round(profit_floor, 2), round(min_roi, 1)


def validate_fba_competition(monthly_sales: int, competitive_fba: int) -> Tuple[bool, str, float]:
    """
    Rotational sales validation:
    - 0 Competitors: 100% share (Total Velocity / 1)
    - 1 Competitor: 50% split (Total Velocity / 2)
    - 2-5 Competitors: Total Velocity / (comp + 1)
    - Rejects anything > 5 competitors or total monthly sales < 30 units/mo.
    """
    comp = max(0, int(competitive_fba or 0))
    sales = int(monthly_sales or 0)

    if comp > MAX_COMPETITIVE_FBA:
        return False, f"FBA competition ({comp}) exceeds max limit of {MAX_COMPETITIVE_FBA}", 0.0

    total_rotation = comp + 1
    our_share = round(sales / total_rotation, 1)

    if sales < 30:
        return False, f"Monthly velocity {sales} below absolute 30 u/mo minimum", 0.0

    if sales <= 249:
        if comp > 3:
            return False, f"Tier (<=249 u/mo) allows max 3 FBA sellers (found {comp})", our_share
    else:
        if comp > 5:
            return False, f"Tier (250+ u/mo) allows max 5 FBA sellers (found {comp})", our_share
        if our_share < 50.0:
            return False, f"Tier (250+ u/mo) requires our share >= 50 u/mo (calculated {our_share:.1f})", our_share

    return True, "QUALIFIED", our_share
