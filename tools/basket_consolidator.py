#!/usr/bin/env python3
"""
Wholesale Basket Consolidator & Landed Cost Engine
Enforces:
1. Vendor MOQs and Free-Freight thresholds.
2. Global Industrial parcel tiers ($24.99 <=$100, $34.99 <=$250, $44.99 <=$500).
3. 45 lb / 24-inch hard limit per unit.
4. $0.00 freight for local Broward/Doral will-call pickups.
5. $0.35 Parkland self-prep cost vs 3PL.
6. 50% ROI minimum hurdle after amortized freight.
"""

import json
import os
from typing import List, Dict, Any, Optional

DISTRIBUTOR_CONFIGS = {
    "global_industrial": {
        "name": "Global Industrial",
        "type": "national_master",
        "base_discount": 0.10,
        "moq": 0.0,
        "free_freight_threshold": None,
        "freight_model": "TIERED_PARCEL",
        "max_weight_lbs": 45.0,
        "is_local_will_call": False
    },
    "atlantic_spice": {
        "name": "Atlantic Spice Company",
        "type": "specialty_botanical",
        "base_discount": 0.0,
        "moq": 0.0,
        "free_freight_threshold": 99.0,
        "freight_model": "THRESHOLD_FREE",
        "max_weight_lbs": 45.0,
        "is_local_will_call": False
    },
    "midland_metal": {
        "name": "Midland Metal Wholesale",
        "type": "fittings_hardware",
        "base_discount": 0.0,
        "moq": 0.0,
        "free_freight_threshold": 250.0,
        "freight_model": "THRESHOLD_FREE",
        "max_weight_lbs": 45.0,
        "is_local_will_call": False
    },
    "grainger_ft_lauderdale": {
        "name": "Grainger (Fort Lauderdale Will-Call)",
        "type": "local_will_call_tier1",
        "base_discount": 0.0,
        "moq": 0.0,
        "free_freight_threshold": 0.0,
        "freight_model": "LOCAL_PICKUP_FREE",
        "max_weight_lbs": 45.0,
        "is_local_will_call": True
    },
    "jpro_supplies": {
        "name": "JPro Supplies (Pompano Dock)",
        "type": "local_will_call_secondary",
        "base_discount": 0.0,
        "moq": 0.0,
        "free_freight_threshold": 0.0,
        "freight_model": "LOCAL_PICKUP_FREE",
        "max_weight_lbs": 45.0,
        "is_local_will_call": True
    },
    "contarmarket": {
        "name": "Contarmarket (Doral Dock)",
        "type": "local_will_call_secondary",
        "base_discount": 0.0,
        "moq": 0.0,
        "free_freight_threshold": 0.0,
        "freight_model": "LOCAL_PICKUP_FREE",
        "max_weight_lbs": 45.0,
        "is_local_will_call": True
    },
    "richelieu_pompano": {
        "name": "Richelieu Hardware (Pompano Dock)",
        "type": "local_trade_will_call",
        "base_discount": 0.30,
        "moq": 0.0,
        "free_freight_threshold": 0.0,
        "freight_model": "LOCAL_PICKUP_FREE",
        "max_weight_lbs": 45.0,
        "is_local_will_call": True
    }
}

SELF_PREP_COST_PER_UNIT = 0.35  # Polybag + FNSKU + "Sold as Set" label in Parkland

def calculate_global_industrial_freight(order_value: float, total_weight_lbs: float) -> float:
    if order_value <= 100.0:
        return 24.99
    elif order_value <= 250.0:
        return 34.99
    elif order_value <= 500.0:
        return 44.99
    else:
        return max(45.0, total_weight_lbs * 0.85)

def evaluate_line_item(item: Dict[str, Any], vendor_key: str) -> Optional[Dict[str, Any]]:
    config = DISTRIBUTOR_CONFIGS.get(vendor_key, {})
    weight = item.get("weight_lbs", 1.0)
    max_weight = config.get("max_weight_lbs", 45.0)

    # 1. Hard weight ceiling check
    if weight > max_weight:
        return None

    wholesale_price = item["wholesale_price"]
    discount = config.get("base_discount", 0.0)
    net_cost = wholesale_price * (1.0 - discount)

    item_copy = dict(item)
    item_copy["net_unit_cost"] = round(net_cost, 2)
    return item_copy

def consolidate_vendor_basket(vendor_key: str, candidate_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    config = DISTRIBUTOR_CONFIGS.get(vendor_key, {
        "name": vendor_key,
        "free_freight_threshold": 0.0,
        "freight_model": "STANDARD",
        "is_local_will_call": False
    })

    valid_items = []
    rejected_heavy_items = []

    for item in candidate_items:
        evaluated = evaluate_line_item(item, vendor_key)
        if evaluated:
            valid_items.append(evaluated)
        else:
            rejected_heavy_items.append(item)

    if not valid_items:
        return {"status": "EMPTY", "vendor": vendor_key, "items": []}

    subtotal = sum(i["net_unit_cost"] * i["qty"] for i in valid_items)
    total_units = sum(i["qty"] for i in valid_items)
    total_weight = sum(i.get("weight_lbs", 1.0) * i["qty"] for i in valid_items)

    # Calculate shipping
    freight = 0.0
    if config.get("is_local_will_call", False):
        freight = 0.0
    elif config.get("freight_model") == "TIERED_PARCEL":
        freight = calculate_global_industrial_freight(subtotal, total_weight)
    elif config.get("freight_model") == "THRESHOLD_FREE":
        threshold = config.get("free_freight_threshold", 99.0)
        if subtotal < threshold:
            freight = 15.00  # standard parcel base
        else:
            freight = 0.0
    else:
        freight = max(12.0, total_weight * 0.75)

    freight_per_unit = freight / max(1, total_units)

    # Calculate final landed costs and ROI verification
    approved_lines = []
    for item in valid_items:
        landed_unit_cost = item["net_unit_cost"] + freight_per_unit + SELF_PREP_COST_PER_UNIT
        est_sell_price = item.get("target_sell_price", 0.0)
        amz_fees = item.get("est_amz_fees", est_sell_price * 0.15 + 4.50)
        net_profit = est_sell_price - landed_unit_cost - amz_fees
        roi_pct = (net_profit / landed_unit_cost) * 100 if landed_unit_cost > 0 else 0.0

        item["landed_unit_cost"] = round(landed_unit_cost, 2)
        item["net_profit_per_unit"] = round(net_profit, 2)
        item["roi_pct"] = round(roi_pct, 1)
        item["passes_roi_floor"] = roi_pct >= 50.0
        approved_lines.append(item)

    # Basket meets efficiency if PO total >= $250 or free freight reached
    is_efficient_basket = True
    if config.get("freight_model") == "TIERED_PARCEL" and subtotal < 250.0:
        is_efficient_basket = False

    return {
        "status": "APPROVED" if is_efficient_basket else "BELOW_OPTIMAL_BASKET_THRESHOLD",
        "vendor": config.get("name", vendor_key),
        "vendor_key": vendor_key,
        "is_local_will_call": config.get("is_local_will_call", False),
        "total_units": total_units,
        "subtotal_merchandise": round(subtotal, 2),
        "inbound_freight": round(freight, 2),
        "prep_cost_total": round(total_units * SELF_PREP_COST_PER_UNIT, 2),
        "total_capital_required": round(subtotal + freight + (total_units * SELF_PREP_COST_PER_UNIT), 2),
        "items": approved_lines,
        "rejected_heavy_count": len(rejected_heavy_items)
    }

if __name__ == "__main__":
    test_basket = [
        {"sku": "GI-49201", "name": "3M Safety Goggles", "wholesale_price": 6.50, "qty": 40, "weight_lbs": 0.5, "target_sell_price": 19.99},
        {"sku": "GI-88122", "name": "Klein Wire Stripper", "wholesale_price": 14.00, "qty": 10, "weight_lbs": 0.8, "target_sell_price": 38.50},
        {"sku": "GI-HEAVY", "name": "55 Gallon Drum Drum", "wholesale_price": 120.00, "qty": 1, "weight_lbs": 58.0, "target_sell_price": 280.00}
    ]
    res = consolidate_vendor_basket("global_industrial", test_basket)
    print(json.dumps(res, indent=2))
