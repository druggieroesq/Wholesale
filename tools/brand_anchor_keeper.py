#!/usr/bin/env python3
"""
Automated Brand Anchor Keeper & Staggered Anti-Regating Pulse Daemon
Features:
1. Tracks only brands that required formal approval and were ungated.
2. Creates FBM Anchor Listing (Qty=0) on Seller Central to hold backend permissions.
3. Staggered Jitter Architecture:
   - Eliminates predictable batching at day 170.
   - Deterministic brand-specific pulse window: 145 to 168 days (randomly distributed by brand hash).
   - Dynamic pulse duration: 10 to 18 days (instead of a static 14-day block).
4. Realistic price guardrail: 1.85x to 2.15x Buy Box with cent randomization (.49, .79, .95).
5. 14-day handling time valve on Dormancy-Keeper-Template.
6. Automatically reverts Qty back to 0 when brand-specific pulse duration expires.
"""

import json
import os
import datetime
import hashlib
from typing import Dict, Any, List

REGISTRY_FILE = "data/brand_ungate_registry.json"

def calculate_brand_pulse_interval(brand_name: str, min_days: int = 145, max_days: int = 168) -> int:
    """Computes a deterministic, staggered cycle interval per brand (145-168 days)."""
    h = int(hashlib.sha256(brand_name.encode('utf-8')).hexdigest(), 16)
    return min_days + (h % (max_days - min_days + 1))

def calculate_brand_pulse_duration(brand_name: str, min_duration: int = 10, max_duration: int = 18) -> int:
    """Computes a staggered pulse duration per brand (10-18 days)."""
    h = int(hashlib.sha256((brand_name + "_duration").encode('utf-8')).hexdigest(), 16)
    return min_duration + (h % (max_duration - min_duration + 1))

def calculate_dynamic_pulse_price(brand_name: str, ref_price: float) -> float:
    """Calculates realistic uncompetitive price (1.85x - 2.15x) with natural ending digits."""
    h = int(hashlib.sha256((brand_name + "_price").encode('utf-8')).hexdigest(), 16)
    multiplier = 1.85 + ((h % 30) / 100.0)  # 1.85 to 2.14
    raw_price = ref_price * multiplier
    cents_options = [0.49, 0.79, 0.89, 0.95, 0.99]
    selected_cents = cents_options[h % len(cents_options)]
    return round(int(raw_price) + selected_cents, 2)

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
    pulse_price = calculate_dynamic_pulse_price(brand_name, ref_price)

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

        # Brand-specific staggered cadence
        staggered_interval = calculate_brand_pulse_interval(brand)
        pulse_duration = calculate_brand_pulse_duration(brand)

        # Case 1: Revert active pulse after brand duration expires
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
                    "duration_days": (today - (end_date - datetime.timedelta(days=pulse_duration))).days,
                    "payload": payload
                })
        # Case 2: Trigger new pulse when reaching brand-specific staggered interval
        elif days_since_pulse >= staggered_interval:
            payload = generate_sp_api_listing_payload(brand, info, "START_PULSE")
            info["active_pulse"] = True
            info["pulse_end_date"] = str(today + datetime.timedelta(days=pulse_duration))
            actions_taken.append({
                "action": "PULSE_ACTIVATED_STAGGERED",
                "brand": brand,
                "days_since_last": days_since_pulse,
                "target_interval": staggered_interval,
                "active_duration_days": pulse_duration,
                "payload": payload
            })

    save_registry(registry)
    return actions_taken

if __name__ == "__main__":
    results = evaluate_and_run_pulse()
    print(f"Executed staggered pulse evaluation. Actions count: {len(results)}")
    print(json.dumps(results, indent=2))
