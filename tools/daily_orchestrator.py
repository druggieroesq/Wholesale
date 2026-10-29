#!/usr/bin/env python3
"""
Daily Master Orchestrator
Executes daily wholesale workflow:
1. Ingests raw distributor price feeds (with discount & weight limits).
2. Runs the 170-Day Brand Anchor Keeper daemon to defend existing brand ungates.
3. Evaluates zero-seller vacuum candidates using Two-Tier routing.
4. Groups approved orders into consolidated distributor baskets.
5. Emits daily Morning Memo summary to terminal / telegram.
"""

import os
import json
import datetime
from tools.basket_consolidator import consolidate_vendor_basket
from tools.brand_anchor_keeper import evaluate_and_run_pulse
from tools.master_vacuum_engine import evaluate_vacuum_opportunity

def run_daily_pipeline():
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    print("==================================================")
    print(f"  RUNNING WHOLESALE SOURCING PIPELINE: {today_str}")
    print("==================================================")

    # Step 1: Run Anti-Regating Pulse Daemon
    print("[1/3] Running Brand Anchor Keeper Daemon...")
    anchor_actions = evaluate_and_run_pulse()
    print(f"      Pulse actions executed: {len(anchor_actions)}")

    # Step 2: Sourcing Scan (Two-Tier Evaluation)
    print("[2/3] Scanning Catalog & Routing Candidates...")
    sample_scanned_items = [
        {
            "asin": "B01N4V71L2",
            "brand": "Klein Tools",
            "title": "Dual Range Non-Contact Voltage Tester",
            "sell_price": 28.50,
            "weight_lbs": 0.4,
            "offers": [
                {"vendor_name": "Global Industrial", "vendor_tier": "tier1_master", "vendor_type": "national_master_tiered", "wholesale_price": 12.00},
                {"vendor_name": "Grainger Fort Lauderdale", "vendor_tier": "tier1_master", "vendor_type": "local_will_call_tier1", "wholesale_price": 13.50}
            ]
        },
        {
            "asin": "B0798C12XX",
            "brand": "Spray Nine",
            "title": "Heavy Duty Cleaner Disinfectant 32oz",
            "sell_price": 22.95,
            "weight_lbs": 2.2,
            "offers": [
                {"vendor_name": "Global Industrial", "vendor_tier": "tier1_master", "vendor_tier_type": "national_master_tiered", "wholesale_price": 8.50},
                {"vendor_name": "JPro Supplies Pompano", "vendor_tier": "tier2_secondary", "vendor_type": "local_will_call_secondary", "wholesale_price": 5.75}
            ]
        }
    ]

    routed_recommendations = []
    for item in sample_scanned_items:
        res = evaluate_vacuum_opportunity(
            asin=item["asin"],
            brand=item["brand"],
            title=item["title"],
            target_sell_price=item["sell_price"],
            weight_lbs=item["weight_lbs"],
            vendor_offers=item["offers"]
        )
        routed_recommendations.append(res)

    # Step 3: Consolidate PO Baskets
    print("[3/3] Generating Consolidated Vendor Purchase Orders...")
    gi_items = [
        {"sku": "GI-KL-100", "name": "Klein Voltage Tester", "wholesale_price": 12.00, "qty": 25, "weight_lbs": 0.4, "target_sell_price": 28.50}
    ]
    gi_basket = consolidate_vendor_basket("global_industrial", gi_items)

    print("\n---------------- MORNING ACTION MEMO ----------------")
    print(f"Brand Protection Status: {len(anchor_actions)} listings pulsed.")
    for rec in routed_recommendations:
        if "selected_vendor" in rec:
            print(f"- [{rec['action_type']}] {rec['title']} ({rec['asin']})")
            print(f"  Routing -> {rec['selected_vendor']} | Net Profit: ${rec['net_profit']} (ROI: {rec['roi_pct']}%)")
    
    print("\nDistributor Basket Status:")
    print(f"- {gi_basket['vendor']}: Status={gi_basket['status']} | Subtotal=${gi_basket.get('subtotal_merchandise', 0.0)} | Freight=${gi_basket.get('inbound_freight', 0.0)}")
    print("-----------------------------------------------------")

if __name__ == "__main__":
    run_daily_pipeline()
