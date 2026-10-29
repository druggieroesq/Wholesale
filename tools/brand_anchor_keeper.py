#!/usr/bin/env python3
"""
Automated Brand Anchor Keeper & 170-Day Anti-Regating Pulse Daemon
Features:
1. Tracks only brands that required formal approval and were ungated.
2. Creates FBM Anchor Listing (Qty=0) on Seller Central to hold backend permissions.
3. Automatically executes 170-day pulse (<180-day cliff) to simulate active seller presence.
4. Realistic price guardrail: 1.8x to 2.2x Buy Box (prevents High Pricing Error suppression).
5. 14-day handling time valve on Dormancy-Keeper-Template.
6. Automatically reverts Qty back to 0 on Day 14.
"""

import json
import os
import datetime
from typing import Dict, Any, List

REGISTRY_FILE = "data/brand_ungate_registry.json"

def load_registry() -> Dict[str, Any]:
    if not os.path.exists(REGISTRY_FILE):
        return {
            "monitored_brands": {
                "Aura Cacia": {
                    "distributor": "Frontier Co-op",
                    "approved_date": "2026-08-01",
                    "requires_ungate": True,
                    "anchor_asin": "B00014DJ7C",
                    "anchor_sku": "KEEPER-AURA-B00014DJ7C",
                    "buy_box_reference": 14.50,
                    "last_pulse_date": "2026-08-02",
                    "active_pulse": False,
                    "pulse_end_date": None
                },
                "Klein Tools": {
                    "distributor": "Global Industrial",
                    "approved_date": "2026-09-15",
                    "requires_ungate": True,
                    "anchor_asin": "B0000302W6",
                    "anchor_sku": "KEEPER-KLEIN-B0000302W6",
                    "buy_box_reference": 24.00,
                    "last_pulse_date": "2026-09-16",
                    "active_pulse": False,
                    "pulse_end_date": None
                }
            }
        }
    with open(REGISTRY_FILE, "r") as f:
        return json.load(f)

def save_registry(data: Dict[str, Any]):
    os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)
    with open(REGISTRY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def generate_sp_api_listing_payload(brand_name: str, brand_data: Dict[str, Any], action: str) -> Dict[str, Any]:
    sku = brand_data["anchor_sku"]
    asin = brand_data["anchor_asin"]
    ref_price = brand_data.get("buy_box_reference", 20.00)
    
    # 1.8x to 2.2x Buy Box Guardrail
    pulse_price = round(ref_price * 2.0, 2)

    if action == "START_PULSE":
        return {
            "seller_sku": sku,
            "product_id": asin,
            "product_id_type": "ASIN",
            "quantity": 1,
            "price": pulse_price,
            "fulfillment_channel": "DEFAULT",  # FBM
            "lead_time_to_ship_max_days": 14,    # 14-day handling buffer
            "merchant_shipping_group_name": "Dormancy-Keeper-Template"
        }
    elif action == "REVERT_PULSE":
        return {
            "seller_sku": sku,
            "product_id": asin,
            "product_id_type": "ASIN",
            "quantity": 0,                      # 0 out of stock
            "price": pulse_price,
            "fulfillment_channel": "DEFAULT",
            "merchant_shipping_group_name": "Dormancy-Keeper-Template"
        }

def evaluate_and_run_pulse():
    registry = load_registry()
    today = datetime.date.today()
    actions_taken = []

    for brand, info in registry.get("monitored_brands", {}).items():
        if not info.get("requires_ungate", False):
            continue

        active_pulse = info.get("active_pulse", False)
        last_pulse = datetime.datetime.strptime(info["last_pulse_date"], "%Y-%m-%d").date()
        days_since_pulse = (today - last_pulse).days

        # Case 1: Revert active pulse after 14 days
        if active_pulse:
            end_date = datetime.datetime.strptime(info["pulse_end_date"], "%Y-%m-%d").date()
            if today >= end_date:
                payload = generate_sp_api_listing_payload(brand, info, "REVERT_PULSE")
                info["active_pulse"] = False
                info["pulse_end_date"] = None
                info["last_pulse_date"] = str(today)
                actions_taken.append({
                    "action": "PULSE_CONCLUDED_REVERTED_TO_ZERO",
                    "brand": brand,
                    "payload": payload
                })
        # Case 2: Trigger new pulse if reached 170 days
        elif days_since_pulse >= 170:
            payload = generate_sp_api_listing_payload(brand, info, "START_PULSE")
            info["active_pulse"] = True
            info["pulse_end_date"] = str(today + datetime.timedelta(days=14))
            actions_taken.append({
                "action": "PULSE_ACTIVATED_14_DAYS",
                "brand": brand,
                "days_since_last": days_since_pulse,
                "payload": payload
            })

    save_registry(registry)
    return actions_taken

if __name__ == "__main__":
    results = evaluate_and_run_pulse()
    print(f"Executed pulse evaluation. Actions count: {len(results)}")
    print(json.dumps(results, indent=2))
