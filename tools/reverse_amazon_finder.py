#!/usr/bin/env python3
"""
Reverse Amazon Finder (24/7 Background Loop)
Continuously queries Keepa's Product Finder API for sub-65k BSR, >= $22 Buy Box,
low-competition FBA opportunities. Enforces zero-token IP Guard verification on all
surfaced ASINs, automatically refills tokens on a 900s interval, and logs qualified items.
"""

import os
import sys
import time
import json
import logging
import requests
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.ip_guard import is_brand_blocked

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ReverseFinder")

DATA_DIR = Path("/app/data")
CANDIDATES_FILE = DATA_DIR / "discovered_candidates.json"
ENV_FILE = Path("/app/.env")


def load_env():
    if not os.getenv("KEEPA_API_KEY") and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("KEEPA_API_KEY=") and not line.startswith("#"):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ["KEEPA_API_KEY"] = key
                break


def query_keepa():
    load_env()
    api_key = os.getenv("KEEPA_API_KEY", "").strip()
    if not api_key:
        logger.error("KEEPA_API_KEY missing from environment and /app/.env")
        return []

    url = f"https://api.keepa.com/query?key={api_key}&domain=1&type=product"
    payload = {
        "current_SALES_lte": 65000,
        "current_BUY_BOX_SHIPPING_gte": 2200,
        "fba_seller_count_lte": 1,
        "isAmazonOffer": False
    }

    try:
        resp = requests.post(url, json=payload, timeout=45)
        if resp.status_code == 200:
            data = resp.json()
            asins = data.get("asinList", [])
            tokens_left = data.get("tokensLeft", "N/A")
            logger.info(f"Keepa query succeeded: {len(asins)} candidate ASINs surfaced. Tokens remaining: {tokens_left}")
            return asins[:50]
        else:
            logger.error(f"Keepa query failed ({resp.status_code}): {resp.text[:160]}")
    except requests.RequestException as e:
        logger.error(f"Keepa connection error: {e}")

    return []


def hydrate_and_verify_asins(api_key: str, asins: list) -> list:
    if not asins:
        return []

    url = f"https://api.keepa.com/product?key={api_key}&domain=1&asin={','.join(asins)}&stats=0"
    verified_candidates = []

    try:
        resp = requests.get(url, timeout=45)
        if resp.status_code == 200:
            products = resp.json().get("products", [])
            for p in products:
                asin = p.get("asin")
                brand = p.get("brand", "")
                title = p.get("title", "")

                blocked, reason = is_brand_blocked(brand=brand, title=title)
                if blocked:
                    logger.info(f"IP Shield: Rejected ASIN {asin} ({reason})")
                    continue

                verified_candidates.append({
                    "asin": asin,
                    "brand": brand,
                    "title": title,
                    "discovered_at": time.time(),
                    "status": "QUALIFIED_UNMATCHED"
                })
        else:
            logger.error(f"Keepa hydration failed ({resp.status_code})")
    except Exception as e:
        logger.error(f"Keepa verification error: {e}")

    return verified_candidates


def update_candidate_pool(new_items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if CANDIDATES_FILE.exists():
        try:
            existing = json.loads(CANDIDATES_FILE.read_text())
        except Exception:
            existing = []

    known_asins = {item.get("asin") for item in existing if isinstance(item, dict)}
    added_count = 0

    for item in new_items:
        asin = item.get("asin")
        if asin and asin not in known_asins:
            existing.append(item)
            known_asins.add(asin)
            added_count += 1

    CANDIDATES_FILE.write_text(json.dumps(existing, indent=2))
    logger.info(f"Candidate pool saved: +{added_count} verified ASINs ({len(existing)} total in pool).")


def main():
    logger.info("Starting Reverse Amazon Finder 24/7 background loop with IP Guard verification...")
    while True:
        raw_asins = query_keepa()
        if raw_asins:
            api_key = os.getenv("KEEPA_API_KEY", "").strip()
            verified = hydrate_and_verify_asins(api_key, raw_asins)
            if verified:
                update_candidate_pool(verified)

        logger.info("Cycle complete. Sleeping 900s for token replenishment...")
        time.sleep(900)


if __name__ == "__main__":
    main()
