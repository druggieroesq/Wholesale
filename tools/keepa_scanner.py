import os
import sys
import json
import logging
import requests
from typing import List, Dict, Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("KeepaScanner")

class KeepaScanner:
    """
    Live Keepa Query Scanner targeted at Commercial MRO, Industrial,
    Beauty, Automotive, and Specialty Spices for 0-seller brand vacuums.
    """
    API_URL = "https://api.keepa.com/query"

    CATEGORY_TARGETS = {
        "industrial_scientific": "16310091",
        "janitorial_sanitation": "16310161",
        "automotive_tools": "15684181",
        "commercial_kitchen": "284507",
        "beauty_professional": "11055981",
        "grocery_gourmet_spices": "16310101"
    }

    def __init__(self):
        self.api_key = os.getenv("KEEPA_API_KEY", "").strip()
        if not self.api_key:
            logger.warning("[KeepaScanner] KEEPA_API_KEY is unset. Running in mock/dry-run mode.")

    def build_query_payload(self, root_category: str) -> Dict[str, Any]:
        return {
            "categories_include": [root_category],
            "current_COUNT_NEW_gte": 0,
            "current_COUNT_NEW_lte": 0,
            "current_SALES_gte": 1,
            "current_SALES_lte": 120000,
            "hasReviews": True,
            "isAdultProduct": False,
            "buyBoxSellerId": "none",
            "sort": [["current_SALES", "asc"]],
            "perPage": 50
        }

    def scan_category(self, cat_name: str, cat_id: str) -> List[Dict[str, Any]]:
        if not self.api_key:
            return self.get_mock_candidates(cat_name)

        logger.info(f"Querying Keepa API for category: {cat_name} (ID: {cat_id})")
        params = {
            "key": self.api_key,
            "domain": "1",  # US domain
            "selection": json.dumps(self.build_query_payload(cat_id))
        }

        try:
            res = requests.get(self.API_URL, params=params, timeout=30)
            if res.status_code != 200:
                logger.error(f"Keepa query failed ({res.status_code}): {res.text}")
                return []
            
            data = res.json()
            asins = data.get("asinList", [])
            logger.info(f"Found {len(asins)} zero-seller candidates in {cat_name}")
            return [{"asin": asin, "category": cat_name, "raw_data": {}} for asin in asins]
        except Exception as e:
            logger.error(f"Failed to fetch Keepa data: {e}")
            return []

    def get_mock_candidates(self, cat_name: str) -> List[Dict[str, Any]]:
        logger.info(f"[Dry-Run] Returning benchmark candidates for {cat_name}")
        benchmark_catalog = {
            "industrial_scientific": [
                {"asin": "B08N5XYZ12", "mpn": "VALVE-BRASS-50", "title": "Industrial Brass Shut-Off Ball Valve 1/2 Inch NPT", "est_sales_rank": 35400, "amz_price": 38.50, "vendor": "global_industrial", "cost": 12.20},
                {"asin": "B09ABC7711", "mpn": "FLT-IND-HEPA-2PK", "title": "MRO Commercial Heavy Duty Replacement Air Filter Pack of 2", "est_sales_rank": 42100, "amz_price": 54.00, "vendor": "global_industrial", "cost": 18.90}
            ],
            "janitorial_sanitation": [
                {"asin": "B07Z89MRO1", "mpn": "MOP-HD-COMM-6PK", "title": "Commercial Looped-End String Wet Mop Head Replacement 6-Pack", "est_sales_rank": 19200, "amz_price": 42.99, "vendor": "global_industrial", "cost": 14.10}
            ],
            "automotive_tools": [
                {"asin": "B01AUTO449", "mpn": "SKT-IMPACT-DEEP-8", "title": "1/2-Inch Drive Deep Impact Metric Socket Set 8-Piece Cr-V", "est_sales_rank": 28400, "amz_price": 32.95, "vendor": "jb_tools", "cost": 11.20}
            ],
            "grocery_gourmet_spices": [
                {"asin": "B00SPICE99", "mpn": "SPICE-CIN-BULK-1LB", "title": "Organic Korintje Cinnamon Powder Bulk 1 Pound Bag", "est_sales_rank": 14500, "amz_price": 18.75, "vendor": "atlantic_spice", "cost": 5.40}
            ]
        }
        return benchmark_catalog.get(cat_name, benchmark_catalog["industrial_scientific"])

    def run_full_scan(self) -> List[Dict[str, Any]]:
        all_leads = []
        for cat_name, cat_id in self.CATEGORY_TARGETS.items():
            leads = self.scan_category(cat_name, cat_id)
            all_leads.extend(leads)
        logger.info(f"Total Keepa raw leads discovered across sectors: {len(all_leads)}")
        return all_leads


if __name__ == "__main__":
    scanner = KeepaScanner()
    results = scanner.run_full_scan()
    print(f"\nDiscovered {len(results)} candidate records.")
    if results:
        print(f"Sample Candidate: {json.dumps(results[0], indent=2)}")
