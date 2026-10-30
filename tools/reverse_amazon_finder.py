import os
import time
import json
import logging
import requests
from pathlib import Path
from typing import Dict, Any, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ReverseFinder")

KEEPA_KEY = os.getenv("KEEPA_API_KEY", "")
MIN_ROI = float(os.getenv("MIN_ROI", "1.00"))
MAX_FBA_SELLERS = int(os.getenv("MAX_FBA_SELLERS", "1"))
MAX_BSR = int(os.getenv("MAX_BSR", "65000"))

CANDIDATES_FILE = Path("/app/data/discovered_candidates.json")

def query_keepa_product_finder() -> List[Dict[str, Any]]:
    """Finds high-demand Amazon products with 0-1 FBA competition and strong sales rank."""
    url = f"https://api.keepa.com/query?key={KEEPA_KEY}&domain=1&type=product"
    
    # Query: Sub-65k BSR, BuyBox >= $22, Max 1 FBA seller, In Stock
    payload = {
        "current_SALES_lte": MAX_BSR,
        "current_BUY_BOX_SHIPPING_gte": 2200,
        "current_COUNT_REVIEWS_gte": 15,
        "fba_seller_count_lte": MAX_FBA_SELLERS,
        "isAmazonOffer": False
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=45)
        if resp.status_code == 200:
            asins = resp.json().get("asinList", [])
            logger.info(f"Keepa Reverse Finder surfaced {len(asins)} high-velocity candidate ASINs.")
            return asins[:50]  # Take top 50 per cycle to conserve tokens
        logger.error(f"Keepa query failed: {resp.status_code}")
    except Exception as e:
        logger.error(f"Keepa query error: {e}")
    return []

def inspect_asin_variations(asins: List[str]):
    """Pulls full variation families and inspects multipacks without discarding on single-pack margin."""
    if not asins:
        return
        
    url = f"https://api.keepa.com/product?key={KEEPA_KEY}&domain=1&asin={','.join(asins[:20])}&stats=90"
    try:
        resp = requests.get(url, timeout=45)
        if resp.status_code != 200:
            return
            
        data = resp.json().get("products", [])
        candidates = []
        if CANDIDATES_FILE.exists():
            try:
                candidates = json.loads(CANDIDATES_FILE.read_text())
            except Exception:
                candidates = []
                
        existing_asins = {c.get("asin") for c in candidates}
        
        for p in data:
            asin = p.get("asin")
            title = p.get("title", "")
            brand = p.get("brand", "")
            mpn = p.get("mpn", "")
            parent_asin = p.get("parentAsin", asin)
            variations = p.get("variations", [])
            
            # Skip if meltable keywords present
            if any(kw in title.lower() for kw in ["chocolate", "gummy", "wax", "balm"]):
                continue
                
            entry = {
                "asin": asin,
                "parent_asin": parent_asin,
                "title": title,
                "brand": brand,
                "mpn": mpn,
                "has_variations": len(variations) > 0,
                "variation_count": len(variations),
                "timestamp": time.time()
            }
            
            if asin not in existing_asins:
                candidates.append(entry)
                existing_asins.add(asin)
                
        CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
        CANDIDATES_FILE.write_text(json.dumps(candidates, indent=2))
        logger.info(f"Discovered candidates pool updated. Total pool: {len(candidates)} items.")
    except Exception as e:
        logger.error(f"Error inspecting variations: {e}")

if __name__ == "__main__":
    logger.info("Starting background reverse finder loop...")
    asins = query_keepa_product_finder()
    if asins:
        inspect_asin_variations(asins)
