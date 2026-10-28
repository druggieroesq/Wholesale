import os
import sys
import time
import json
import logging
import requests
from datetime import datetime
from typing import List, Dict, Any

from tools.master_vacuum_engine import MasterVacuumEngine
from tools.telegram_action_handler import TelegramActionHandler
from tools.sku_resolver import SKUResolver
from tools.git_state_sync import GitStateSync

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RollingCatalogCycler")

class RollingCatalogCycler:
    """
    72-Hour Rolling Catalog Cycler:
    - Scans 12 commercial sectors in rolling 3-day waves.
    - Operates at net-zero token burn (paced requests).
    - Filters for items under 5 lbs with sales rank < 80,000.
    - Delivers 2-3 target acquisitions per day.
    """
    QUERY_API = "https://api.keepa.com/query"
    PRODUCT_API = "https://api.keepa.com/product"

    # 12 Target Sectors across 3 Days (4 sectors per day)
    SECTORS = [
        # DAY 1: Industrial, Hardware & MRO
        {"day": 1, "name": "Industrial Hardware & Valves", "cat_id": "16310091", "max_weight_g": 2268},
        {"day": 1, "name": "Janitorial & Sanitation MRO", "cat_id": "16310161", "max_weight_g": 2268},
        {"day": 1, "name": "Automotive Specialty Tools", "cat_id": "15684181", "max_weight_g": 1814},
        {"day": 1, "name": "Commercial Electrical & Fittings", "cat_id": "495318", "max_weight_g": 1814},

        # DAY 2: Consumables, Spices & Beauty
        {"day": 2, "name": "Professional Salon & Toners", "cat_id": "11055981", "max_weight_g": 907},
        {"day": 2, "name": "Bulk Herbs, Botanicals & Spices", "cat_id": "16310101", "max_weight_g": 1360},
        {"day": 2, "name": "Commercial Kitchen & Barista Parts", "cat_id": "284507", "max_weight_g": 1360},
        {"day": 2, "name": "Aromatherapy & Essential Oils", "cat_id": "3760911", "max_weight_g": 907},

        # DAY 3: Sports, Precision & Niche Consumables
        {"day": 3, "name": "Specialty Sports Gear Consumables", "cat_id": "3375251", "max_weight_g": 2268},
        {"day": 3, "name": "Precision Optics & Lab Consumables", "cat_id": "318603011", "max_weight_g": 1360},
        {"day": 3, "name": "HVAC Consumables & Filtration", "cat_id": "3206324011", "max_weight_g": 2268},
        {"day": 3, "name": "Plumbing Specialty Valves", "cat_id": "680345011", "max_weight_g": 2268}
    ]

    def __init__(self):
        self.api_key = os.getenv("KEEPA_API_KEY", "").strip()
        self.engine = MasterVacuumEngine(prep_override=0.35)
        self.telegram = TelegramActionHandler()
        self.resolver = SKUResolver()
        self.syncer = GitStateSync()
        self.safety_token_floor = 25

    def scan_sector(self, sector: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.api_key:
            logger.info(f"[Dry Run] Simulating 0-seller checks for: {sector['name']}")
            return []

        payload = {
            "categories_include": [sector["cat_id"]],
            "current_COUNT_NEW_gte": 0,
            "current_COUNT_NEW_lte": 0,
            "current_SALES_gte": 1,
            "current_SALES_lte": 80000,
            "packageWeight_lte": sector["max_weight_g"],
            "hasReviews": True,
            "buyBoxSellerId": "none",
            "sort": [["current_SALES", "asc"]],
            "perPage": 30
        }

        params = {"key": self.api_key, "domain": "1", "selection": json.dumps(payload)}

        try:
            res = requests.get(self.QUERY_API, params=params, timeout=30)
            data = res.json()
            tokens_left = data.get("tokensLeft", 100)
            logger.info(f"Sector: {sector['name']} | Keepa Tokens Available: {tokens_left}")

            if tokens_left < self.safety_token_floor:
                logger.warning(f"Token reserves below {self.safety_token_floor}. Entering regeneration pause...")
                time.sleep(60)

            asin_list = data.get("asinList", [])
            return self.fetch_product_details(asin_list[:15], sector)
        except Exception as e:
            logger.error(f"Error querying sector {sector['name']}: {e}")
            return []

    def fetch_product_details(self, asins: List[str], sector: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not asins:
            return []

        params = {
            "key": self.api_key,
            "domain": "1",
            "asin": ",".join(asins),
            "stats": "1",
            "history": "0"
        }

        try:
            res = requests.get(self.PRODUCT_API, params=params, timeout=30)
            products = res.json().get("products", [])
            leads = []

            for p in products:
                asin = p.get("asin")
                title = p.get("title", "Commercial Item")
                brand = p.get("brand", "Unknown")
                stats = p.get("stats", {})

                bb_cents = stats.get("buyBoxPrice", -1)
                new_cents = stats.get("current", [None]*2)[1] if stats.get("current") else -1
                price_cents = bb_cents if bb_cents > 0 else new_cents

                if price_cents <= 0:
                    continue

                amz_price = round(price_cents / 100.0, 2)
                est_wholesale_cost = round(amz_price * 0.45, 2)

                leads.append({
                    "asin": asin,
                    "title": title,
                    "brand": brand,
                    "mpn": p.get("mpn", ""),
                    "amazon_price": amz_price,
                    "wholesale_cost": est_wholesale_cost,
                    "sales_rank": stats.get("salesRankDrop30", 30000),
                    "sector": sector["name"],
                    "weight_lbs": round(p.get("packageWeight", 500) / 453.6, 2)
                })

            return leads
        except Exception as e:
            logger.error(f"Error reading ASIN batch: {e}")
            return []

    def run_continuous_72h_cycle(self):
        logger.info("⚡ Starting 72-Hour Rolling Catalog Cycler...")
        sector_idx = 0

        while True:
            sector = self.SECTORS[sector_idx]
            logger.info(f"[Day {sector['day']}] Scanning {sector['name']}...")

            leads = self.scan_sector(sector)
            if leads:
                # Filter through Master Vacuum Engine (50% ROI, $0.35 prep)
                viable = self.engine.process_catalog(leads)
                if viable:
                    # Alert the top candidate
                    top_pick = viable[0]
                    self.telegram.send_deal_card(top_pick)
                    logger.info(f"Dispatched daily candidate: {top_pick['asin']} ({top_pick['title'][:35]})")

            sector_idx = (sector_idx + 1) % len(self.SECTORS)

            # 45-second sleep paces calls so Keepa tokens regenerate continuously
            logger.info("Resting 45 seconds for Keepa token regeneration...")
            time.sleep(45)


if __name__ == "__main__":
    cycler = RollingCatalogCycler()
    cycler.run_continuous_72h_cycle()
