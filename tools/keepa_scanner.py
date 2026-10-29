import os
import sys
import json
import logging
import requests
import pandas as pd
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("KeepaScanner")

MASTER_CATALOG_PATH = "data/catalogs/master_distributor_index.csv"

# Physical thresholds for Amazon FBA Small Parcel Delivery
MAX_WEIGHT_GRAMS = 20411    # 45.0 lbs ceiling (strict buffer under Amazon 50.0 lb limit)
MAX_DIMENSION_CM = 60.96    # 24.0 inches ceiling (strict buffer under Amazon 25.0 inch limit)

class KeepaScanner:
    """
    High-volume Keepa Scanner:
    1. Loads indexed distributor items from master catalog.
    2. Batches queries up to 100 codes/ASINs per API call.
    3. Strictly enforces Amazon FBA physical thresholds (<=45 lbs, <=24 inches).
    4. Detects zero-seller vacuum conditions.
    """
    KEEPA_PRODUCT_API = "https://api.keepa.com/product"

    def __init__(self):
        self.api_key = os.getenv("KEEPA_API_KEY", "").strip()
        if not self.api_key:
            logger.warning("[KeepaScanner] No KEEPA_API_KEY detected. Running in simulated dry-run mode.")

    def load_catalog(self) -> pd.DataFrame:
        if os.path.exists(MASTER_CATALOG_PATH):
            return pd.read_csv(MASTER_CATALOG_PATH)
        return pd.DataFrame()

    def batch_query_keepa(self, identifiers: List[str], id_type: str = "code") -> Dict[str, Any]:
        if not self.api_key or not identifiers:
            return {}

        params = {
            "key": self.api_key,
            "domain": "1",
            "stats": "1",
            "history": "0"
        }

        if id_type == "code":
            params["code"] = ",".join(identifiers[:100])
        else:
            params["asin"] = ",".join(identifiers[:100])

        try:
            res = requests.get(self.KEEPA_PRODUCT_API, params=params, timeout=30)
            if res.status_code == 200:
                return res.json()
            else:
                logger.error(f"Keepa API returned {res.status_code}: {res.text}")
                return {}
        except Exception as e:
            logger.error(f"Error querying Keepa: {e}")
            return {}

    def parse_keepa_product(self, prod: Dict[str, Any], catalog_match: Dict[str, Any]) -> Dict[str, Any]:
        asin = prod.get("asin", "")
        title = prod.get("title", catalog_match.get("title", ""))

        # Physical limit checks (grams and mm)
        pkg_weight_g = prod.get("packageWeight", 0)
        pkg_length_mm = prod.get("packageLength", 0)
        pkg_width_mm = prod.get("packageWidth", 0)
        pkg_height_mm = prod.get("packageHeight", 0)

        weight_lbs = round(pkg_weight_g / 453.592, 2) if pkg_weight_g else 0.0
        max_dim_inches = round(max(pkg_length_mm, pkg_width_mm, pkg_height_mm) / 25.4, 2) if any([pkg_length_mm, pkg_width_mm, pkg_height_mm]) else 0.0

        if pkg_weight_g and pkg_weight_g > MAX_WEIGHT_GRAMS:
            logger.debug(f"Rejecting {asin}: Weight {weight_lbs} lbs exceeds Amazon limit.")
            return {}

        if any(dim > (MAX_DIMENSION_CM * 10) for dim in [pkg_length_mm, pkg_width_mm, pkg_height_mm]):
            logger.debug(f"Rejecting {asin}: Dimension {max_dim_inches} in exceeds Amazon limit.")
            return {}

        stats = prod.get("stats", {})
        current_new_sellers = stats.get("current", [None]*12)[11] if stats.get("current") else None

        bb_price_cents = stats.get("buyBoxPrice", -1)
        if bb_price_cents <= 0:
            new_price_cents = stats.get("current", [None]*2)[1] if stats.get("current") else -1
            amz_price = float(new_price_cents) / 100.0 if new_price_cents and new_price_cents > 0 else 0.0
        else:
            amz_price = float(bb_price_cents) / 100.0

        sales_rank = stats.get("salesRankDrop30", 0)
        is_vacuum = (current_new_sellers == 0) or (amz_price > 0 and current_new_sellers is None)

        return {
            "asin": asin,
            "title": title,
            "vendor": catalog_match.get("vendor"),
            "mpn": catalog_match.get("mpn"),
            "upc": catalog_match.get("upc"),
            "cost": float(catalog_match.get("wholesale_cost", 0.0)),
            "amz_price": amz_price,
            "est_sales_rank": sales_rank,
            "current_sellers": current_new_sellers if current_new_sellers is not None else 0,
            "is_vacuum": is_vacuum,
            "package_weight_lbs": weight_lbs,
            "max_dimension_inches": max_dim_inches
        }

    def scan_master_catalog(self) -> List[Dict[str, Any]]:
        df = self.load_catalog()
        if df.empty:
            logger.warning("Master catalog is empty. Run universal_catalog_ingestor first.")
            return []

        logger.info(f"Scanning across {len(df)} indexed distributor catalog records...")

        if not self.api_key:
            sample = []
            for _, r in df.iterrows():
                cost = float(r["wholesale_cost"])
                sim_amz_price = round(cost * 2.8, 2)
                sample.append({
                    "asin": f"B0{str(r['vendor'])[:3].upper()}{str(r['sku'])[:4]}",
                    "title": str(r["title"]),
                    "vendor": str(r["vendor"]),
                    "mpn": str(r["mpn"]),
                    "upc": str(r["upc"]),
                    "cost": cost,
                    "amz_price": sim_amz_price,
                    "est_sales_rank": 22000,
                    "current_sellers": 0,
                    "is_vacuum": True,
                    "package_weight_lbs": 2.5,
                    "max_dimension_inches": 8.0
                })
            return sample

        leads = []
        upc_items = df[df["upc"].astype(str).str.len() >= 10].to_dict(orient="records")
        chunk_size = 100

        for i in range(0, len(upc_items), chunk_size):
            chunk = upc_items[i:i + chunk_size]
            upc_list = [str(c["upc"]) for c in chunk]
            response = self.batch_query_keepa(upc_list, id_type="code")

            products = response.get("products", [])
            for p in products:
                prod_upc = p.get("upcList", [""])[0] if p.get("upcList") else ""
                matched_row = next((item for item in chunk if str(item["upc"]) == str(prod_upc)), chunk[0])
                lead = self.parse_keepa_product(p, matched_row)
                if lead and lead.get("is_vacuum") and lead.get("amz_price", 0) > lead.get("cost", 0):
                    leads.append(lead)

        return leads

    def run_full_scan(self, *args, **kwargs) -> List[Dict[str, Any]]:
        """Invoked by the sniper loop background cycle."""
        return self.scan_master_catalog(*args, **kwargs)


if __name__ == "__main__":
    scanner = KeepaScanner()
    found = scanner.run_full_scan()
    print(f"Discovered {len(found)} candidate opportunities within FBA limits.")
