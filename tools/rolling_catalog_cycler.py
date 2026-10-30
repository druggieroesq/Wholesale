"""
tools/rolling_catalog_cycler.py
Phased Sourcing Cycler for Keepa API (Starter 20 Tokens/Min Compliant).
- Phase 1: Deep scan across Industrial & Scientific and Tools & Hardware.
- Phase 2: Cycles into Commercial Consumables, Safety, and Plumbing.
- Enforces Supply Vacuums: Amazon OOS, FBA sellers <= 1, Buy Box >= $30.
- No BSR rank caps — driven by verified demand and dynamic MIN_ROI_FLOOR.
"""

import os
import sys
import time
import json
import logging
import requests
from pathlib import Path
from typing import List, Dict, Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env", override=True)

from tools.master_vacuum_engine import MasterVacuumEngine
from tools.telegram_action_handler import TelegramActionHandler
from tools.sku_resolver import SKUResolver
from tools.ip_guard import is_brand_blocked
from tools.logistics_engine import LogisticsEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] RollingCycler: %(message)s")
logger = logging.getLogger("RollingCycler")

MAX_FBA_WEIGHT_GRAMS = 20411    # 45.0 lbs ceiling
MAX_FBA_DIMENSION_MM = 609      # 24.0 inches ceiling


class RollingCatalogCycler:
    QUERY_API = "https://api.keepa.com/query"
    PRODUCT_API = "https://api.keepa.com/product"

    PRIMARY_PHASE_1_SECTORS = [
        {"phase": 1, "name": "Industrial Hardware, Fasteners & Valves", "cat_id": "16310091", "max_weight_g": 6800},
        {"phase": 1, "name": "Industrial & Scientific Supplies (MRO)", "cat_id": "16310161", "max_weight_g": 9070},
        {"phase": 1, "name": "Power & Hand Tools Commercial", "cat_id": "228013", "max_weight_g": 6800},
        {"phase": 1, "name": "Commercial Electrical Fittings & Conduit", "cat_id": "495318", "max_weight_g": 4536},
        {"phase": 1, "name": "Pneumatic & Hydraulic Equipment", "cat_id": "383599011", "max_weight_g": 6800},
        {"phase": 1, "name": "Industrial Janitorial & Sanitation", "cat_id": "16310181", "max_weight_g": 9070}
    ]

    EXPANSION_PHASE_2_SECTORS = [
        {"phase": 2, "name": "Commercial HVAC & Air Quality", "cat_id": "3206324011", "max_weight_g": 4536},
        {"phase": 2, "name": "Commercial Plumbing Specialty", "cat_id": "680345011", "max_weight_g": 4536},
        {"phase": 2, "name": "Commercial Kitchen & Foodservice Parts", "cat_id": "284507", "max_weight_g": 3628},
        {"phase": 2, "name": "Specialty Consumables & Lab Ware", "cat_id": "318603011", "max_weight_g": 2268}
    ]

    def __init__(self):
        self.api_key = os.getenv("KEEPA_API_KEY", "").strip()
        self.engine = MasterVacuumEngine()
        self.telegram = TelegramActionHandler()
        self.resolver = SKUResolver()
        self.logistics = LogisticsEngine()
        self.safety_token_floor = int(os.getenv("KEEPA_TOKEN_SAFETY_FLOOR", 15))
        self.active_phase = 1

    def scan_sector_vacuums(self, sector: Dict[str, Any]) -> List[str]:
        if not self.api_key or self.api_key == "your_actual_keepa_key":
            logger.warning(f"[Dry Run] Keepa API Key not configured. Skipping live scan for {sector['name']}.")
            return []

        weight_cap_g = min(sector.get("max_weight_g", MAX_FBA_WEIGHT_GRAMS), MAX_FBA_WEIGHT_GRAMS)

        payload = {
            "categories_include": [sector["cat_id"]],
            "current_AMAZON": -1,              # Amazon 1P is completely Out of Stock
            "current_COUNT_NEW_lte": 1,        # 0 or 1 competitor (Supply Vacuum)
            "avg90_BUY_BOX_gte": 3000,         # Minimum $30 Buy Box price
            "packageWeight_lte": weight_cap_g,
            "packageLength_lte": MAX_FBA_DIMENSION_MM,
            "packageWidth_lte": MAX_FBA_DIMENSION_MM,
            "packageHeight_lte": MAX_FBA_DIMENSION_MM,
            "hasReviews": True,
            "sort": [["current_SALES", "asc"]],
            "perPage": 25
        }

        params = {"key": self.api_key, "domain": "1", "selection": json.dumps(payload)}

        try:
            res = requests.get(self.QUERY_API, params=params, timeout=30)
            data = res.json()
            tokens_left = data.get("tokensLeft", 100)
            logger.info(f"Sector: {sector['name']} | Keepa Tokens Left: {tokens_left}")

            if tokens_left < self.safety_token_floor:
                logger.warning(f"Tokens low ({tokens_left}). Pausing 60s to regenerate...")
                time.sleep(60)

            return data.get("asinList", [])[:15]
        except Exception as e:
            logger.error(f"Error querying sector {sector['name']}: {e}")
            return []

    def hydrate_and_screen_asins(self, asins: List[str], sector: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not asins:
            return []

        params = {
            "key": self.api_key,
            "domain": "1",
            "asin": ",".join(asins),
            "stats": "90",
            "history": "0"
        }

        try:
            res = requests.get(self.PRODUCT_API, params=params, timeout=30)
            products = res.json().get("products", [])
            screened_leads = []

            for p in products:
                asin = p.get("asin")
                title = p.get("title", "")
                brand = p.get("brand", "")
                stats = p.get("stats", {}) or {}

                blocked, reason = is_brand_blocked(brand=brand, title=title)
                if blocked:
                    continue

                pkg_g = p.get("packageWeight", 0)
                if pkg_g > MAX_FBA_WEIGHT_GRAMS:
                    continue

                bb_cents = stats.get("buyBoxPrice", -1)
                new_cents = stats.get("current", [None] * 2)[1] if stats.get("current") else -1
                price_cents = bb_cents if bb_cents > 0 else new_cents
                if price_cents <= 0:
                    continue

                amz_price = round(price_cents / 100.0, 2)
                weight_lbs = round(pkg_g / 453.6, 2) if pkg_g else 1.5

                route = self.logistics.route_purchase_order(
                    vendor_key="regional_supplier",
                    unit_cost=amz_price * 0.35,
                    weight_lbs=weight_lbs
                )
                overhead = route["total_fulfillment_overhead"]

                # Target cost calculated against dynamic ROI floor
                target_roi = self.engine.min_roi_floor / 100.0
                fba_fee = self.engine.estimate_fba_fee(weight_lbs)
                net_ceiling = (amz_price * (1.0 - self.engine.AMAZON_REFERRAL_PCT)) - fba_fee - ((1.0 + target_roi) * overhead)
                max_allowable_cost = max(0.0, round(net_ceiling / (1.0 + target_roi), 2))

                if max_allowable_cost < 3.0:
                    continue

                screened_leads.append({
                    "asin": asin,
                    "title": title,
                    "brand": brand,
                    "mpn": p.get("mpn", ""),
                    "amazon_price": amz_price,
                    "cost": max_allowable_cost,
                    "max_allowable_cost": max_allowable_cost,
                    "fba_sellers": stats.get("totalOfferCountFBA", 0),
                    "amazon_in_stock": False,
                    "sector": sector["name"],
                    "package_weight_lbs": weight_lbs,
                    "max_dimension_inches": round(max(p.get("packageLength", 0), p.get("packageWidth", 0), p.get("packageHeight", 0)) / 25.4, 2)
                })

            return screened_leads
        except Exception as e:
            logger.error(f"Error hydrating ASIN batch: {e}")
            return []

    def run_phased_sourcing_loop(self):
        logger.info("Initializing Phased Sourcing Cycler...")
        sectors_queue = self.PRIMARY_PHASE_1_SECTORS.copy()
        current_idx = 0

        while True:
            sector = sectors_queue[current_idx]
            logger.info(f"[Phase {sector['phase']}] Scanning sector: {sector['name']}")

            candidate_asins = self.scan_sector_vacuums(sector)
            if candidate_asins:
                leads = self.hydrate_and_screen_asins(candidate_asins, sector)
                if leads:
                    viable = self.engine.process_catalog(leads)
                    if viable:
                        top_pick = viable[0]
                        self.telegram.send_deal_card(top_pick)
                        logger.info(f"Dispatched Deal: {top_pick['brand']} | {top_pick['asin']} (MAC: ${top_pick['wholesale_cost']})")

            current_idx += 1
            if current_idx >= len(sectors_queue):
                current_idx = 0
                if self.active_phase == 1:
                    logger.info("Phase 1 complete. Expanding to Phase 2 sectors.")
                    sectors_queue.extend(self.EXPANSION_PHASE_2_SECTORS)
                    self.active_phase = 2

            sleep_duration = 35
            logger.info(f"Pacing loop: sleeping {sleep_duration}s for token regeneration...")
            time.sleep(sleep_duration)


if __name__ == "__main__":
    cycler = RollingCatalogCycler()
    cycler.run_phased_sourcing_loop()
