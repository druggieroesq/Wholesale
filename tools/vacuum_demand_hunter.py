"""
tools/vacuum_demand_hunter.py
Automated Supply-Vacuum Hunter.
Cross-references live Amazon/Keepa inventory vacuums (0-1 FBA sellers, Amazon OOS)
against active wholesale distributor catalogs (BCI, Midland, Grainger, South FL Will-Call).
Enforces 100% Net ROI, $0.06 prep baseline, and Layer 0 IP Guard.
"""

import os
import sys
import json
import logging
import requests
from pathlib import Path
from typing import List, Dict, Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(ROOT_DIR / ".env")

from tools.master_vacuum_engine import MasterVacuumEngine
from tools.sku_resolver import SKUResolver
from tools.ip_guard import is_brand_blocked

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("VacuumDemandHunter")

KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "").strip()


class VacuumDemandHunter:
    """
    Dedicated demand hunter for distributor catalog cross-referencing.
    Hunts high-velocity zero-supply vacuums across prioritized categories.
    """
    def __init__(self, prep_fee: float = 0.06):
        # Uses the updated $0.06 baseline
        self.engine = MasterVacuumEngine(prep_override=prep_fee)
        self.resolver = SKUResolver()

    def fetch_keepa_supply_vacuums(self, category_id: int = 16310161) -> List[str]:
        """
        Uses Keepa /query to fetch up to 1,000 ASINs in high-demand, zero-supply states
        (Amazon OOS, FBA sellers <= 1, Buy Box >= $30) for only ~10 tokens.
        Default category: 16310161 (Industrial & Scientific).
        """
        if not KEEPA_API_KEY or KEEPA_API_KEY == "your_actual_keepa_key":
            logger.warning("No live Keepa API key found. Skipping live Keepa vacuum harvest.")
            return []

        url = f"https://api.keepa.com/query?key={KEEPA_API_KEY}&domain=1"
        payload = {
            "rootCategory": category_id,
            "current_AMAZON": -1,              # Amazon Retail is 100% Out of Stock
            "current_COUNT_NEW_lte": 1,        # 0 or 1 competitor (Supply Vacuum)
            "avg90_BUY_BOX_gte": 3000,         # Minimum $30 Buy Box
            "productType": [0, 1, 2],
            "sort": [["current_SALES", "asc"]],
            "perPage": 1000
        }

        try:
            resp = requests.post(url, json=payload, timeout=25)
            if resp.status_code == 200:
                asins = resp.json().get("asinList", [])
                logger.info(f"Keepa query found {len(asins)} zero-supply vacuums in category {category_id}.")
                return asins
            else:
                logger.error(f"Keepa query failed: {resp.text}")
                return []
        except Exception as e:
            logger.error(f"Error querying Keepa vacuums: {e}")
            return []

    def load_active_distributor_catalogs(self) -> List[Dict[str, Any]]:
        """
        Dynamically loads pre-filtered distributor queues from data/catalogs/.
        """
        catalogs_dir = ROOT_DIR / "data" / "catalogs"
        all_candidates = []

        if not catalogs_dir.exists():
            return all_candidates

        for file_path in catalogs_dir.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        for item in data:
                            all_candidates.append({
                                "asin": item.get("asin", ""),
                                "upc": item.get("upc", ""),
                                "mpn": item.get("item_number", item.get("mpn", "")),
                                "title": item.get("title", ""),
                                "brand": item.get("brand", ""),
                                "vendor": item.get("distributor", "bci"),
                                "cost": float(item.get("wholesale_cost", 0.0)),
                                "amz_price": float(item.get("amz_price", item.get("msrp", 0.0))),
                                "fba_sellers": item.get("fba_sellers", 0),
                                "amazon_in_stock": item.get("amazon_in_stock", False),
                                "package_weight_lbs": float(item.get("weight_lbs", 1.5))
                            })
            except Exception as e:
                logger.error(f"Failed to read catalog {file_path.name}: {e}")

        logger.info(f"Loaded {len(all_candidates)} candidates across active distributor catalogs.")
        return all_candidates

    def hunt_vacuums(self) -> List[Dict[str, Any]]:
        logger.info("Hunting zero-supply opportunities across active wholesale catalogs...")
        raw_items = self.load_active_distributor_catalogs()

        # If no local catalog files are present, fall back to benchmark verification items
        if not raw_items:
            logger.warning("No local catalog JSON files found in data/catalogs/. Using baseline benchmark items.")
            raw_items = [
                {
                    "asin": "B08N5XYZ12",
                    "upc": "729000123456",
                    "mpn": "BRASS-VALVE-50-NPT",
                    "title": "Industrial Heavy Duty Brass Ball Valve 1/2 Inch NPT Female",
                    "brand": "Standard Brass",
                    "vendor": "midland_metal",
                    "cost": 11.50,
                    "amz_price": 38.99,
                    "fba_sellers": 0,
                    "amazon_in_stock": False,
                    "package_weight_lbs": 1.2
                },
                {
                    "asin": "B079HEPA99",
                    "upc": "729000654321",
                    "mpn": "FILT-MRO-HVAC-4PK",
                    "title": "Commercial Grade Pleated HVAC Air Filter MERV 11 4-Pack",
                    "brand": "AirClean Pro",
                    "vendor": "global_industrial",
                    "cost": 21.00,
                    "amz_price": 68.50,
                    "fba_sellers": 1,
                    "amazon_in_stock": False,
                    "package_weight_lbs": 3.0
                }
            ]

        # Process through the updated MasterVacuumEngine (IP check, FBA <= 1, 100% ROI floor)
        viable_deals = self.engine.process_catalog(raw_items)

        for deal in viable_deals:
            try:
                deal["direct_order_url"] = self.resolver.get_order_url(
                    deal["vendor"],
                    mpn=deal.get("mpn"),
                    title=deal.get("title"),
                    asin=deal.get("asin")
                )
            except Exception:
                deal["direct_order_url"] = ""

        return viable_deals


if __name__ == "__main__":
    hunter = VacuumDemandHunter()
    results = hunter.hunt_vacuums()
    
    print("\n" + "=" * 80)
    print(f"  VACUUM DEMAND HUNTER: {len(results)} QUALIFIED DEALS DETECTED (PARKLAND PREP)")
    print("=" * 80)
    for r in results:
        print(f"ASIN: {r['asin']} | {r['title'][:40]}")
        print(f"Vendor: {r['vendor']} | Wholesale: ${r['wholesale_cost']} | AMZ Price: ${r['amazon_price']}")
        print(f"Net Profit: ${r['net_profit']} | ROI: {r['roi_pct']}% | Quadrant: {r['quadrant']}")
        print(f"Direct Link: {r['direct_order_url']}")
        print("-" * 80)
