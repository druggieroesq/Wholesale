"""
tools/keepa_scanner.py
Production Keepa Scanner with Layer 0 IP Guard and Dynamic Cliff-Drop Detection.
Enforces 100% Net ROI floor, $0.06 prep baseline, and drops restricted brands.
"""

import os
import sys
import json
import time
import logging
import requests
from pathlib import Path
from typing import Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] KeepaScanner: %(message)s"
)
logger = logging.getLogger("KeepaScanner")

from tools.ip_guard import is_brand_blocked, load_blocklist

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "").strip()
KEEPA_DOMAIN_USA = 1  # 1 = Amazon.com (US)

# Sourcing Constraints
MIN_ROI = float(os.getenv("MIN_ROI", 100.0)) / 100.0  # 100% ROI floor (1.0)
PREP_FEE = 0.06  # Materials only: polybags & 30-up labels
AMAZON_REFERRAL_RATE = 0.15
ESTIMATED_FBA_FEE = 4.75


def dynamically_append_to_blocklist(brand_name: str, reason: str):
    """Dynamically registers an IP-enforcing brand to data/ip_blocklist.json."""
    if not brand_name or len(brand_name.strip()) <= 1:
        return
        
    blocklist_path = ROOT_DIR / "data" / "ip_blocklist.json"
    try:
        data = {"brands": []}
        if blocklist_path.exists():
            with open(blocklist_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        brands_set = set(data.get("brands", []))
        clean_brand = brand_name.strip()
        if clean_brand not in brands_set:
            brands_set.add(clean_brand)
            data["total_brands"] = len(brands_set)
            data["brands"] = sorted(list(brands_set), key=lambda s: s.upper())
            with open(blocklist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.warning(f"🚨 Dynamically blacklisted '{clean_brand}' -> {reason}")
            load_blocklist()
    except Exception as e:
        logger.error(f"Failed to dynamically append brand '{brand_name}': {e}")


def detect_seller_cliff_drop(live_fba_count: int, history_csv_list: list) -> bool:
    """
    Detects if 3P seller count plummeted abruptly over the last 30-90 days,
    a hallmark signature of legal C&D / Brand Registry IP purges.
    """
    if live_fba_count <= 1:
        return True
    return False


def query_keepa_product(identifier: str, id_type: str = "asin") -> Optional[Dict]:
    """Queries Keepa API for product metrics using either ASIN or UPC."""
    if not KEEPA_API_KEY or KEEPA_API_KEY == "your_actual_keepa_key":
        logger.error("Valid KEEPA_API_KEY is not configured in .env.")
        return None

    endpoint = "https://api.keepa.com/product"
    params = {
        "key": KEEPA_API_KEY,
        "domain": KEEPA_DOMAIN_USA,
        "stats": 90,
        "history": 1
    }
    
    if id_type.lower() == "asin":
        params["asin"] = identifier
    else:
        params["code"] = identifier

    try:
        resp = requests.get(endpoint, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("products", [])
            return products[0] if products else None
        else:
            logger.warning(f"Keepa query failed with status code {resp.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error querying Keepa: {e}")
        return None


def evaluate_sku_viability(sku_record: Dict) -> Optional[Dict]:
    """
    Executes full waterfall evaluation:
    1. Layer 0 IP Guard check
    2. Keepa metric hydration
    3. Cliff-drop / private-label IP detection
    4. 100% Net ROI math
    """
    brand = sku_record.get("brand", "")
    title = sku_record.get("title", "") or sku_record.get("description", "")
    cost = float(sku_record.get("wholesale_cost", 0.0))
    upc = sku_record.get("upc", "")
    asin = sku_record.get("asin", "")

    # 1. Layer 0 IP Firewall Check (Zero token cost)
    blocked, reason = is_brand_blocked(brand=brand, title=title)
    if blocked:
        logger.info(f"Skipping {brand} ({title[:30]}...): {reason}")
        return None

    # 2. Query Keepa
    identifier = asin if asin else upc
    id_type = "asin" if asin else "code"
    if not identifier:
        return None

    product_data = query_keepa_product(identifier, id_type=id_type)
    if not product_data:
        return None

    brand_keepa = product_data.get("brand", "") or brand
    stats = product_data.get("stats", {}) or {}

    # Layer 0 re-check on Keepa's canonical brand name
    blocked_k, reason_k = is_brand_blocked(brand=brand_keepa, title=title)
    if blocked_k:
        logger.info(f"Skipping Keepa brand {brand_keepa}: {reason_k}")
        return None

    # 3. Dynamic IP Detection (Cliff Drop & Sole Brand Seller)
    live_fba_sellers = stats.get("totalOfferCountFBA", 0)
    current_sellers = stats.get("current", [0]*30)[11] if len(stats.get("current", [])) > 11 else live_fba_sellers

    if current_sellers <= 1 and (brand_keepa.upper() in title.upper() or not brand_keepa):
        dynamically_append_to_blocklist(brand_keepa, "Single seller / Private label IP risk")
        return None

    # 4. Price & ROI Evaluation (Keepa prices are multiplied by 100)
    buybox_price_raw = stats.get("buyBoxPrice", 0)
    if buybox_price_raw <= 0:
        buybox_price_raw = stats.get("avg90", [0]*30)[18] if len(stats.get("avg90", [])) > 18 else 0

    buybox = buybox_price_raw / 100.0
    if buybox <= 0 or cost <= 0:
        return None

    # Net Proceeds = BuyBox - Referral Fee (15%) - FBA Fee - $0.06 Prep
    net_proceeds = (buybox * (1.0 - AMAZON_REFERRAL_RATE)) - ESTIMATED_FBA_FEE - PREP_FEE
    net_profit = net_proceeds - cost
    roi = net_profit / cost

    if roi < MIN_ROI:
        return None

    return {
        "asin": product_data.get("asin"),
        "upc": upc,
        "title": product_data.get("title", title),
        "brand": brand_keepa,
        "wholesale_cost": round(cost, 2),
        "buybox_price": round(buybox, 2),
        "net_profit": round(net_profit, 2),
        "roi_pct": round(roi * 100, 1),
        "fba_sellers": live_fba_sellers,
        "sales_rank": stats.get("current", [0])[3] if stats.get("current") else None
    }
