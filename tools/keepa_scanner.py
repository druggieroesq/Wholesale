"""
tools/keepa_scanner.py
Production Keepa Batch Scanner with Dynamic Self-Learning IP Quarantine.
Enforces 100% Net ROI floor, $0.06 prep baseline, and permanent brand blacklisting.
"""

import os
import sys
import json
import time
import logging
import requests
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

# Sourcing Constraints & Cost Parameters
MIN_ROI = float(os.getenv("MIN_ROI", 100.0)) / 100.0  # 100% Net ROI floor
PREP_FEE = 0.06  # Polybag, hazard warning label & FNSKU barcode
AMAZON_REFERRAL_RATE = 0.15
ESTIMATED_FBA_FEE = 4.75

BLOCKLIST_FILE = ROOT_DIR / "data" / "ip_blocklist.json"


def quarantine_brand_globally(brand_name: str, asin: str, trigger_reason: str):
    """
    Permanently appends a detected IP-enforcing brand to data/ip_blocklist.json on disk.
    Immediately locks out the brand across ALL catalogs and sourcing engines.
    """
    clean_brand = (brand_name or "").strip()
    if not clean_brand or len(clean_brand) < 2:
        return

    try:
        data = {"brands": []}
        if BLOCKLIST_FILE.exists():
            with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

        existing_brands = {b.strip().upper() for b in data.get("brands", [])}

        if clean_brand.upper() not in existing_brands:
            data.setdefault("brands", []).append(clean_brand)
            data["total_brands"] = len(data["brands"])
            data["brands"] = sorted(list(set(data["brands"])), key=lambda s: s.upper())

            with open(BLOCKLIST_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            logger.warning(
                f"🚨 [AUTO-IP QUARANTINE] ASIN {asin} triggered lockdown for '{clean_brand.upper()}'. "
                f"Reason: {trigger_reason}. Master blocklist updated ({data['total_brands']} brands)."
            )

            # Instantly reload IP Guard memory cache
            load_blocklist()

    except Exception as e:
        logger.error(f"Failed to auto-quarantine brand '{clean_brand}': {e}")


def analyze_keepa_ip_signals(product: Dict) -> Tuple[bool, str]:
    """
    Inspects Keepa product metrics and historical offer trends for IP enforcement.
    Returns: (is_ip_suspicious, reason_string)
    """
    stats = product.get("stats", {}) or {}
    brand = (product.get("brand") or "").strip()
    title = product.get("title") or ""
    asin = product.get("asin") or ""

    # Keepa stats arrays index 11 = COUNT_NEW (Offer count)
    current_arr = stats.get("current", [])
    avg90_arr = stats.get("avg90", [])
    avg30_arr = stats.get("avg30", [])

    current_sellers = current_arr[11] if len(current_arr) > 11 and current_arr[11] is not None else 0
    avg_90_sellers = avg90_arr[11] if len(avg90_arr) > 11 and avg90_arr[11] is not None else 0
    avg_30_sellers = avg30_arr[11] if len(avg30_arr) > 11 and avg30_arr[11] is not None else 0

    # 1. Cliff Drop: Healthy seller pool collapsed to <= 1 (Brand Registry / C&D sweep)
    if avg_90_sellers >= 5 and current_sellers <= 1:
        reason = f"Seller Cliff Drop (avg90: {avg_90_sellers} sellers -> current: {current_sellers})"
        quarantine_brand_globally(brand, asin, reason)
        return True, reason

    # 2. Private Label Lock: Sole seller historical dominance
    if avg_90_sellers <= 1 and current_sellers <= 1:
        if brand.upper() in title.upper() and len(brand) > 3:
            reason = "Private Label Monopoly (sole seller over 90-day history)"
            quarantine_brand_globally(brand, asin, reason)
            return True, reason

    # 3. Sudden 30-Day Purge: Offers collapsed by >70% recently
    if avg_90_sellers >= 4 and avg_30_sellers <= 1:
        reason = f"Recent 30-Day Offer Purge (avg90: {avg_90_sellers} -> avg30: {avg_30_sellers})"
        quarantine_brand_globally(brand, asin, reason)
        return True, reason

    return False, ""


def query_keepa_batch(identifiers: List[str], id_type: str = "asin") -> List[Dict]:
    """
    Batch-queries up to 100 ASINs or UPCs in a single Keepa API call to conserve tokens.
    """
    if not KEEPA_API_KEY or KEEPA_API_KEY == "your_actual_keepa_key":
        logger.error("Valid KEEPA_API_KEY is not configured in .env.")
        return []

    endpoint = "https://api.keepa.com/product"
    params = {
        "key": KEEPA_API_KEY,
        "domain": KEEPA_DOMAIN_USA,
        "stats": 90,
        "history": 1
    }

    if id_type.lower() == "asin":
        params["asin"] = ",".join(identifiers)
    else:
        params["code"] = ",".join(identifiers)

    try:
        resp = requests.get(endpoint, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            tokens_left = data.get("tokensLeft", "N/A")
            logger.info(f"Keepa query successful. Tokens remaining: {tokens_left}")
            return data.get("products", [])
        else:
            logger.error(f"Keepa query failed with HTTP {resp.status_code}: {resp.text}")
            return []
    except Exception as e:
        logger.error(f"Exception during Keepa batch request: {e}")
        return []


def process_candidate_batch(sku_records: List[Dict]) -> List[Dict]:
    """
    Processes a list of raw distributor records through the complete multi-tier pipeline:
    1. Layer 0 IP Guard check against the 659-brand blocklist (0 tokens spent)
    2. Keepa batch hydration (up to 100 items per request)
    3. Live Keepa IP heuristic analysis + auto-quarantine
    4. 100% Net ROI floor math
    """
    viable_deals = []

    # 1. Pre-filter against static blocklist
    pre_screened = []
    for item in sku_records:
        brand = item.get("brand", "")
        title = item.get("title", "")
        vendor = item.get("vendor", "")

        blocked, reason = is_brand_blocked(brand=brand, title=title, vendor=vendor)
        if blocked:
            continue
        pre_screened.append(item)

    if not pre_screened:
        logger.info("All candidates in this batch were filtered by Layer 0 IP Guard.")
        return []

    logger.info(f"Pre-screened {len(pre_screened)}/{len(sku_records)} safe items for Keepa scan.")

    # 2. Batch in chunks of 50 to prevent URI length limits
    chunk_size = 50
    for i in range(0, len(pre_screened), chunk_size):
        chunk = pre_screened[i : i + chunk_size]
        
        # Determine identifier strategy (ASIN vs UPC)
        asins = [c.get("asin") for c in chunk if c.get("asin")]
        upcs = [c.get("upc") for c in chunk if not c.get("asin") and c.get("upc")]

        products = []
        if asins:
            products.extend(query_keepa_batch(asins, id_type="asin"))
        if upcs:
            products.extend(query_keepa_batch(upcs, id_type="code"))

        # Map Keepa response back by ASIN and UPC
        keepa_map = {}
        for p in products:
            if p.get("asin"):
                keepa_map[p["asin"]] = p
            upc_list = p.get("upcList", [])
            for u in upc_list:
                keepa_map[u.zfill(12)] = p

        # 3. Analyze viable candidates
        for item in chunk:
            lookup_key = item.get("asin") or item.get("upc", "").zfill(12)
            k_data = keepa_map.get(lookup_key)
            if not k_data:
                continue

            k_brand = k_data.get("brand", "") or item.get("brand", "")
            k_title = k_data.get("title", "") or item.get("title", "")
            cost = float(item.get("wholesale_cost", 0.0))

            # Post-lookup IP check against Keepa's canonical brand name
            blocked, reason = is_brand_blocked(brand=k_brand, title=k_title)
            if blocked:
                continue

            # Live heuristic scan & dynamic quarantine
            is_ip, ip_reason = analyze_keepa_ip_signals(k_data)
            if is_ip:
                continue

            # Extract pricing: Keepa prices are integers in cents ($29.99 = 2999)
            stats = k_data.get("stats", {}) or {}
            buybox_cents = stats.get("buyBoxPrice", 0)
            
            # Fall back to 90-day Buy Box average if current is suppressed
            if buybox_cents <= 0:
                avg90_arr = stats.get("avg90", [])
                buybox_cents = avg90_arr[18] if len(avg90_arr) > 18 and avg90_arr[18] is not None else 0

            buybox = buybox_cents / 100.0 if buybox_cents > 0 else 0.0
            if buybox <= 0 or cost <= 0:
                continue

            # Financial waterfall: Net Profit >= Wholesale Cost (100% Net ROI)
            net_proceeds = (buybox * (1.0 - AMAZON_REFERRAL_RATE)) - ESTIMATED_FBA_FEE - PREP_FEE
            net_profit = net_proceeds - cost
            roi = net_profit / cost

            if roi >= MIN_ROI:
                deal = {
                    "distributor": item.get("distributor", "Unknown"),
                    "item_number": item.get("item_number", ""),
                    "asin": k_data.get("asin"),
                    "upc": item.get("upc", ""),
                    "brand": k_brand,
                    "title": k_title,
                    "wholesale_cost": round(cost, 2),
                    "buybox_price": round(buybox, 2),
                    "net_profit": round(net_profit, 2),
                    "roi_pct": round(roi * 100, 1),
                    "fba_sellers": stats.get("totalOfferCountFBA", 0),
                    "sales_rank": stats.get("current", [0]*5)[3] if len(stats.get("current", [])) > 3 else None
                }
                viable_deals.append(deal)
                logger.info(
                    f"🎯 [MATCHED DEAL] {deal['brand']} | Cost: ${deal['wholesale_cost']} | "
                    f"BB: ${deal['buybox_price']} | Profit: ${deal['net_profit']} | ROI: {deal['roi_pct']}%"
                )

        time.sleep(1.0)  # Rate limiting between Keepa requests

    return viable_deals


if __name__ == "__main__":
    # Test batch execution against prepared BCI queue if present
    bci_queue_path = ROOT_DIR / "data" / "catalogs" / "bci_ready_for_keepa.json"
    if bci_queue_path.exists():
        with open(bci_queue_path, "r", encoding="utf-8") as f:
            candidates = json.load(f)
        logger.info(f"Loaded {len(candidates)} items from {bci_queue_path.name}")
        results = process_candidate_batch(candidates[:10])
        print(f"\nDiscovered {len(results)} deals meeting the 100% ROI floor.")
    else:
        print("Prepare a scan queue or run tools/bci_catalog_parser.py first.")
