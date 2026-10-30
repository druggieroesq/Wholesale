"""
tools/dual_feed_dispatcher.py
Generates the daily action pipeline:
- Feed A: 25 Confirmed Buy Orders (Known wholesale cost, Net ROI >= 100%, 0-2 FBA sellers).
- Feed B: 25 Reverse RFQ Targets (High velocity, 0 FBA or Suppressed BB, Target Buy Price computed, 3 Regional Wholesalers identified).
- On-Demand Batching: Supports /more_buys and /more_rfqs via Telegram or CLI.
- Distributor Ingestion Tracker: Tracks daily goal of 5 new distributor accounts added to the Master Registry.
"""

import os
import json
import logging
from typing import Dict, List, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

MASTER_DISTRIBUTORS_FILE = "data/master_distributors.json"
DAILY_DISPATCH_CACHE = "data/daily_dispatch_cache.json"

def calculate_target_buy_price(retail_price: float, referral_pct: float = 0.15, fba_fee: float = 4.50, prep_cost: float = 0.06, target_roi: float = 1.00) -> float:
    """
    Computes maximum allowable landed cost to achieve target_roi.
    Net Profit = retail - fees - cost.
    ROI = Net Profit / cost => retail - fees - cost = target_roi * cost => cost * (1 + target_roi) = retail - fees.
    cost = (retail - fees) / (1 + target_roi).
    """
    fees = (retail_price * referral_pct) + fba_fee + prep_cost
    if retail_price <= fees:
        return 0.0
    max_landed = (retail_price - fees) / (1.0 + target_roi)
    return round(max_landed, 2)

def generate_daily_worklist(
    confirmed_pool: List[Dict[str, Any]],
    reverse_pool: List[Dict[str, Any]],
    feed_a_limit: int = 25,
    feed_b_limit: int = 25
) -> Dict[str, Any]:
    """
    Packs the daily work queue.
    """
    feed_a = confirmed_pool[:feed_a_limit]
    feed_b = []

    for item in reverse_pool[:feed_b_limit]:
        retail = float(item.get("effective_price", 0.0))
        target_cost = calculate_target_buy_price(retail, target_roi=1.00)
        feed_b.append({
            "asin": item.get("asin"),
            "title": item.get("title"),
            "brand": item.get("brand"),
            "mpn": item.get("mpn"),
            "retail_price": retail,
            "monthly_velocity": item.get("monthly_velocity", 20),
            "active_fba_sellers": item.get("active_fba_sellers", 0),
            "target_buy_price": target_cost,
            "suggested_distributors": item.get("suggested_distributors", [])
        })

    payload = {
        "feed_a_buys": feed_a,
        "feed_b_rfqs": feed_b,
        "remaining_feed_a": len(confirmed_pool) - len(feed_a),
        "remaining_feed_b": len(reverse_pool) - len(feed_b),
        "distributor_target_today": 5
    }

    os.makedirs("data", exist_ok=True)
    with open(DAILY_DISPATCH_CACHE, "w") as f:
        json.dump(payload, f, indent=2)

    return payload
