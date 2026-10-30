#!/usr/bin/env python3
"""
Reverse Amazon Finder (24/7 Background Hunter)
Queries Keepa Product Finder for high-margin, low-competition MRO & multipack targets.
"""

import os
import time
import json
import logging
import requests
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ReverseFinder")

DATA_DIR = Path("/app/data")
CANDIDATES_FILE = DATA_DIR / "discovered_candidates.json"
ENV_FILE = Path("/app/.env")


def load_env():
    """Ensure KEEPA_API_KEY is loaded even in detached non-login subshells."""
    if not os.getenv("KEEPA_API_KEY") and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("KEEPA_API_KEY=") and not line.startswith("#"):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ["KEEPA_API_KEY"] = key
                break


def query_keepa():
    """Query Keepa Product Finder API for qualified high-margin ASINs."""
    load_env()
    api_key = os.getenv("KEEPA_API_KEY", "").strip()
    if not api_key:
        logger.error("KEEPA_API_KEY is missing from environment and /app/.env")
        return []

    # Keepa Product Finder: domain 1 = Amazon.com (US)
    url = f"https://api.keepa.com/query?key={api_key}&domain=1&type=product"
    
    # Query parameters: BSR <= 65k, Buy Box >= $22.00 (in cents), max 1 FBA seller, No Amazon retail
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


def update_candidate_pool(new_asins):
    """Save new ASINs to persistent JSON storage with deduplication."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = []
    if CANDIDATES_FILE.exists():
        try:
            existing = json.loads(CANDIDATES_FILE.read_text())
        except Exception:
            existing = []

    known_asins = {item.get("asin") for item in existing if isinstance(item, dict)}
    added_count = 0
    now = time.time()

    for asin in new_asins:
        if asin and asin not in known_asins:
            existing.append({
                "asin": asin,
                "discovered_at": now,
                "status": "UNMATCHED"
            })
            known_asins.add(asin)
            added_count += 1

    CANDIDATES_FILE.write_text(json.dumps(existing, indent=2))
    logger.info(f"Candidate pool saved: +{added_count} new ASINs ({len(existing)} total in pool).")


def main():
    logger.info("Starting Reverse Amazon Finder 24/7 background loop...")
    while True:
        candidates = query_keepa()
        if candidates:
            update_candidate_pool(candidates)
        
        # Keepa token refill pacing (15-minute wait between cycles)
        logger.info("Cycle complete. Sleeping 900s for token refill...")
        time.sleep(900)


if __name__ == "__main__":
    main()
