"""
tools/bundle_optimizer.py
Calculates optimal multipack build ratios and work orders upon physical inventory arrival.
Uses live Keepa variation metrics, 30-day velocity caps, and unit profit maximization.
"""

import math
from typing import Dict, List, Any

def optimize_bundle_allocation(
    total_units_received: int,
    variations: List[Dict[str, Any]],
    max_days_supply: int = 30,
    min_roi_floor: float = 1.00,
    prep_material_cost_per_bundle: float = 0.06
) -> Dict[str, Any]:
    """
    Allocates received units into multipack bundles (2-pk, 3-pk, etc.)
    Args:
        total_units_received: Integer number of physical base units on the table.
        variations: List of variation dictionaries containing:
            - asin: str
            - pack_size: int (e.g., 2, 3, 4)
            - buy_box_price: float
            - referral_fee: float
            - fba_fee: float
            - base_unit_cost: float
            - monthly_sales_est: int (Keepa estimated drops/month)
        max_days_supply: Ceiling on warehouse velocity (default 30 days).
        min_roi_floor: Minimum required ROI (default 50%).
        prep_material_cost_per_bundle: Default materials cost (poly bag + labels = $0.06).
    """
    analyzed_packs = []

    for v in variations:
        pack_size = int(v.get("pack_size", 1))
        if pack_size <= 1:
            continue

        base_cost = float(v.get("base_unit_cost", 0.0))
        cogs = (base_cost * pack_size) + prep_material_cost_per_bundle
        price = float(v.get("buy_box_price", 0.0))
        fees = float(v.get("referral_fee", price * 0.15)) + float(v.get("fba_fee", 4.50))
        
        net_profit = price - fees - cogs
        roi = (net_profit / cogs) if cogs > 0 else 0.0

        if roi < min_roi_floor or net_profit <= 0:
            continue

        monthly_sales = max(1, int(v.get("monthly_sales_est", 10)))
        daily_velocity = monthly_sales / 30.0
        max_bundles_allowed = max(1, math.floor(daily_velocity * max_days_supply))
        profit_per_base_unit = net_profit / pack_size

        analyzed_packs.append({
            "asin": v.get("asin"),
            "pack_size": pack_size,
            "buy_box_price": price,
            "net_profit": round(net_profit, 2),
            "roi": round(roi * 100, 1),
            "monthly_sales": monthly_sales,
            "max_bundles_allowed": max_bundles_allowed,
            "profit_per_base_unit": profit_per_base_unit
        })

    analyzed_packs.sort(key=lambda x: x["profit_per_base_unit"], reverse=True)

    remaining_units = total_units_received
    allocation = []
    total_profit_projected = 0.0
    total_bundles_built = 0

    for p in analyzed_packs:
        pack_size = p["pack_size"]
        max_by_units = remaining_units // pack_size
        build_bundles = min(max_by_units, p["max_bundles_allowed"])

        if build_bundles > 0:
            units_used = build_bundles * pack_size
            remaining_units -= units_used
            batch_profit = build_bundles * p["net_profit"]
            total_profit_projected += batch_profit
            total_bundles_built += build_bundles

            allocation.append({
                "pack_size": pack_size,
                "asin": p["asin"],
                "bundles_to_build": build_bundles,
                "units_consumed": units_used,
                "buy_box_price": p["buy_box_price"],
                "net_profit_per_bundle": p["net_profit"],
                "total_profit": round(batch_profit, 2),
                "roi_pct": p["roi"]
            })

    return {
        "total_units_received": total_units_received,
        "total_bundles_to_prep": total_bundles_built,
        "allocated_packs": allocation,
        "leftover_single_units": remaining_units,
        "total_projected_profit": round(total_profit_projected, 2),
        "supplies_needed": {
            "poly_bags": total_bundles_built,
            "sold_as_set_labels": total_bundles_built,
            "suffocation_warning_labels": total_bundles_built,
            "fnsku_barcodes": total_bundles_built
        }
    }
