#!/usr/bin/env python3
"""
tools/reorder_po_generator.py
-----------------------------
Inventory Reorder & Pre-PO Sizing Engine.
Calculates 14-day lean replenishment quantities sized strictly to OUR FAIR SHARE of the Buy Box.
"""

import sys
import logging
from pathlib import Path
from typing import Dict, Any, List

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import calculate_sliding_profit_floor, get_logistics_profile

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] ReorderEngine: %(message)s")
logger = logging.getLogger("ReorderEngine")


def size_lean_purchase_order(
    asin: str,
    sku: str,
    title: str,
    vendor: str,
    wholesale_cost: float,
    buy_box_price: float,
    total_monthly_sales: int,
    competitive_fba_sellers: int = 1,
    current_fba_inventory: int = 0
) -> Dict[str, Any]:
    # 1. Fair share rotation
    total_rotation = max(1, competitive_fba_sellers + 1)
    our_monthly_velocity = total_monthly_sales / total_rotation

    # 2. Sliding floor verification
    profit_floor, required_roi = calculate_sliding_profit_floor(our_monthly_velocity)
    logistics = get_logistics_profile(vendor)
    prep_fee = logistics["landed_estimate"]

    referral_fee = buy_box_price * 0.15
    fba_pick_pack = 4.15
    return_buffer = buy_box_price * 0.05
    total_landed = wholesale_cost + prep_fee
    net_profit = buy_box_price - (wholesale_cost + referral_fee + fba_pick_pack + prep_fee + return_buffer)
    roi = (net_profit / total_landed) * 100.0 if total_landed > 0 else 0.0

    # 3. 14-day lean buffer sizing
    two_week_turn_units = max(12, int(our_monthly_velocity * 0.5))
    suggested_reorder_qty = max(0, two_week_turn_units - current_fba_inventory)

    is_viable = (net_profit >= profit_floor) and (roi >= required_roi) and (suggested_reorder_qty > 0)

    return {
        "asin": asin,
        "sku": sku,
        "title": title[:40],
        "vendor": vendor,
        "prep_facility": logistics["facility"],
        "viable_deal": is_viable,
        "suggested_units": suggested_reorder_qty,
        "capital_required": round(suggested_reorder_qty * wholesale_cost, 2),
        "projected_profit": round(suggested_reorder_qty * net_profit, 2),
        "our_monthly_share": int(our_monthly_velocity),
        "net_profit_per_unit": round(net_profit, 2),
        "roi_pct": round(roi, 1),
        "hurdle_applied": f"${profit_floor:.2f} floor / {required_roi:.0f}% ROI"
    }
