import os
import re
import csv
import time
import math
import logging
import requests
import pandas as pd
from pathlib import Path
from typing import Dict, Any, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("UniversalScanner")

# --- Configuration & Environment ---
KEEPA_KEY = os.getenv("KEEPA_API_KEY", "")
MIN_ROI = float(os.getenv("MIN_ROI", "1.00"))             # 100% Net ROI floor
MIN_DOIR = float(os.getenv("MIN_DOIR", "1.20"))           # Capital velocity floor
MAX_FBA_SELLERS = int(os.getenv("MAX_FBA_SELLERS", "1"))   # Max 1 competing FBA seller
MAX_BSR = int(os.getenv("MAX_BSR", "65000"))
ALLOW_HAZMAT = os.getenv("ALLOW_HAZMAT", "false").lower() == "true"

# Fee Buffers
PREP_FEE_PER_BUNDLE = 0.35      # Polybag + suffocation warning + FNSKU label
INBOUND_PLACEMENT_FEE = 0.25    # Amazon Inbound Placement Service buffer per unit
INBOUND_FREIGHT_PER_LB = 0.40   # Partnered carrier freight estimate

# Blacklists
MELTABLE_KEYWORDS = {
    "chocolate", "gummy", "gummies", "wax", "candle", 
    "balm", "butter", "meltable", "gelatin", "softgel"
}

DISTRIBUTOR_RULES = {
    "frontier": {"free_freight": 250.0, "min_order": 100.0},
    "bci": {"free_freight": 1500.0, "min_order": 750.0},
    "contour": {"free_freight": 500.0, "min_order": 200.0}
}

def is_meltable(title: str) -> bool:
    """Pre-filter check: drop heat-sensitive goods immediately (0 tokens)."""
    t = title.lower()
    return any(kw in t for kw in MELTABLE_KEYWORDS)

def keepa_api_request(url: str, params: dict, max_retries: int = 5) -> Optional[dict]:
    """Exponential backoff watchdog for Keepa API resilience."""
    params["key"] = KEEPA_KEY
    wait = 15
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                tokens_left = data.get("tokensLeft", 0)
                refill_in = data.get("refillIn", 0)
                if tokens_left < 10 and refill_in > 0:
                    sleep_sec = math.ceil(refill_in / 1000.0) + 1
                    logger.info(f"Keepa token bucket low ({tokens_left}). Pausing {sleep_sec}s...")
                    time.sleep(sleep_sec)
                return data
            elif resp.status_code in (429, 502, 503, 504):
                logger.warning(f"HTTP {resp.status_code}. Retrying in {wait}s...")
                time.sleep(wait)
                wait = min(wait * 2, 120)
            else:
                logger.error(f"Keepa failed with HTTP {resp.status_code}")
                return None
        except Exception as e:
            logger.warning(f"Connection exception: {e}. Retrying in {wait}s...")
            time.sleep(wait)
            wait = min(wait * 2, 120)
    return None

def estimate_fba_fee(weight_lbs: float, length_in: float, width_in: float, height_in: float) -> float:
    """Calculates dimensional weight tiers to prevent oversized fee creep."""
    dim_weight = (length_in * width_in * height_in) / 139.0
    billable_weight = max(weight_lbs, dim_weight)
    
    is_oversize = (
        max(length_in, width_in, height_in) > 18.0 or
        min(length_in, width_in, height_in) > 8.0 or
        (length_in + 2 * (width_in + height_in)) > 130.0 or
        billable_weight > 20.0
    )
    if is_oversize:
        return 9.80 + max(0.0, billable_weight - 2.0) * 0.45
    else:
        if billable_weight <= 0.5: return 3.22
        elif billable_weight <= 1.0: return 3.86
        elif billable_weight <= 2.0: return 4.90
        elif billable_weight <= 3.0: return 5.45
        else: return 5.75 + math.ceil(billable_weight - 3.0) * 0.35

def evaluate_pack_size(
    buy_box: float, 
    unit_cost: float, 
    pack_qty: int, 
    weight_lbs: float, 
    dims: tuple, 
    fba_count: int, 
    bsr: int, 
    monthly_sales: int
) -> Optional[Dict[str, Any]]:
    """Calculates unit economics decoupled from single-pack constraints."""
    if buy_box <= 0 or pack_qty <= 0 or unit_cost <= 0:
        return None
        
    total_cost = unit_cost * pack_qty
    l, w, h = dims
    total_weight = weight_lbs * pack_qty
    bundle_height = h * (pack_qty if pack_qty <= 3 else math.ceil(pack_qty / 2.0))
    
    referral_fee = max(0.30, buy_box * 0.15)
    fba_fee = estimate_fba_fee(total_weight, l, w, bundle_height)
    inbound_freight = total_weight * INBOUND_FREIGHT_PER_LB
    placement_fee = INBOUND_PLACEMENT_FEE * pack_qty
    prep_fee = PREP_FEE_PER_BUNDLE if pack_qty > 1 else 0.0
    
    total_deductions = total_cost + referral_fee + fba_fee + inbound_freight + placement_fee + prep_fee
    net_profit = buy_box - total_deductions
    roi = (net_profit / total_cost) if total_cost > 0 else 0.0
    doir = roi * (monthly_sales / 30.0)
    
    passes = (roi >= MIN_ROI and doir >= MIN_DOIR and fba_count <= MAX_FBA_SELLERS and bsr <= MAX_BSR)
    
    return {
        "pack_qty": pack_qty,
        "buy_box": round(buy_box, 2),
        "total_cost": round(total_cost, 2),
        "fba_fee": round(fba_fee, 2),
        "referral_fee": round(referral_fee, 2),
        "inbound_buffer": round(inbound_freight + placement_fee + prep_fee, 2),
        "net_profit": round(net_profit, 2),
        "roi": round(roi, 3),
        "doir": round(doir, 3),
        "fba_count": fba_count,
        "bsr": bsr,
        "monthly_velocity": monthly_sales,
        "passes_filters": passes
    }

def optimize_case_pack_allocation(viable_packs: List[Dict[str, Any]], case_qty: int = 1, max_turn_days: int = 40) -> Dict[str, Any]:
    """Snaps order volume to distributor master case multiples and allocates splits by DOIR."""
    if not viable_packs:
        return {"total_units": 0, "splits": []}
        
    raw_demand = sum(((p["monthly_velocity"] / 30.0) * max_turn_days) * p["pack_qty"] for p in viable_packs)
    case_qty = max(1, case_qty)
    cases_needed = max(1, math.ceil(raw_demand / case_qty))
    total_units = cases_needed * case_qty
    
    total_doir = sum(p["doir"] for p in viable_packs)
    splits = []
    
    for p in viable_packs:
        weight = p["doir"] / total_doir if total_doir > 0 else (1.0 / len(viable_packs))
        target_units = total_units * weight
        bundles = int(target_units // p["pack_qty"])
        used = bundles * p["pack_qty"]
        splits.append({
            "pack_size": f"{p['pack_qty']}-Pack",
            "bundles_to_prep": bundles,
            "units_consumed": used,
            "projected_profit": round(bundles * p["net_profit"], 2),
            "roi_percent": f"{int(p['roi'] * 100)}%"
        })
        
    return {
        "total_units_to_order": total_units,
        "cases_to_order": cases_needed,
        "splits": splits
    }

def export_po_csv(distributor_key: str, items: List[Dict[str, Any]], output_path: Path):
    """Exports clean vendor PO CSV with free-freight subtotal tracking."""
    total_spend = sum(i["units"] * i["unit_cost"] for i in items)
    rules = DISTRIBUTOR_RULES.get(distributor_key.lower(), {"free_freight": 250.0, "min_order": 100.0})
    
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["SKU", "OrderQuantity", "UnitCost", "LineTotal"])
        for i in items:
            w.writerow([i["sku"], i["units"], f"{i['unit_cost']:.2f}", f"{(i['units'] * i['unit_cost']):.2f}"])
            
    logger.info(f"Vendor PO written to {output_path} | Spend: ${total_spend:.2f} (Free Freight: ${rules['free_freight']:.2f})")
    if total_spend < rules["free_freight"]:
        logger.warning(f"⚠️ PO is ${rules['free_freight'] - total_spend:.2f} below free freight minimum.")
