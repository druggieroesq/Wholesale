#!/usr/bin/env python3
"""
tools/sp_api_gating.py
----------------------
Live Amazon SP-API Gating & Listing Restrictions Verification Engine.
Queries Amazon Listings Restrictions API (2021-08-01) to verify whether
an ASIN is directly sellable in 'New' condition under FBA/FBM.
Includes 14-day SQLite caching and deal-score bonus weighting.
"""

import os
import sys
import time
import sqlite3
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env", override=False)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] GatingAPI: %(message)s")
logger = logging.getLogger("SP_API_Gating")

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DB = DATA_DIR / "gating_cache.db"

# SP-API Environment Variables
SP_API_REFRESH_TOKEN = os.getenv("SP_API_REFRESH_TOKEN", "").strip()
SP_API_CLIENT_ID = os.getenv("SP_API_CLIENT_ID", "").strip()
SP_API_CLIENT_SECRET = os.getenv("SP_API_CLIENT_SECRET", "").strip()
SELLER_ID = os.getenv("AMAZON_SELLER_ID", "").strip()
MARKETPLACE_ID = os.getenv("AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER").strip()

_CACHED_TOKEN = None
_TOKEN_EXPIRY = 0


def init_cache_db():
    """Initializes local SQLite cache for gating checks."""
    conn = sqlite3.connect(CACHE_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gating_records (
            asin TEXT PRIMARY KEY,
            brand TEXT,
            status TEXT,
            reason TEXT,
            cached_at INTEGER
        )
    """)
    conn.commit()
    conn.close()

init_cache_db()


def get_lwa_access_token() -> Optional[str]:
    """Retrieves and caches LWA access token."""
    global _CACHED_TOKEN, _TOKEN_EXPIRY
    now = time.time()
    if _CACHED_TOKEN and now < _TOKEN_EXPIRY:
        return _CACHED_TOKEN

    if not (SP_API_REFRESH_TOKEN and SP_API_CLIENT_ID and SP_API_CLIENT_SECRET):
        logger.warning("SP-API credentials missing. Gating will run in passive mode.")
        return None

    try:
        url = "https://api.amazon.com/auth/o2/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": SP_API_REFRESH_TOKEN,
            "client_id": SP_API_CLIENT_ID,
            "client_secret": SP_API_CLIENT_SECRET
        }
        res = requests.post(url, data=payload, timeout=10)
        if res.status_code == 200:
            data = res.json()
            _CACHED_TOKEN = data.get("access_token")
            _TOKEN_EXPIRY = now + data.get("expires_in", 3600) - 120
            return _CACHED_TOKEN
        else:
            logger.error(f"Failed LWA token fetch: HTTP {res.status_code} - {res.text}")
            return None
    except Exception as e:
        logger.error(f"Error requesting LWA access token: {e}")
        return None


def get_cached_gating_status(asin: str) -> Optional[Dict[str, Any]]:
    """Checks SQLite cache for 14-day valid gating status."""
    conn = sqlite3.connect(CACHE_DB)
    cur = conn.cursor()
    cur.execute("SELECT status, reason, cached_at FROM gating_records WHERE asin = ?", (asin.upper(),))
    row = cur.fetchone()
    conn.close()

    if row:
        status, reason, cached_at = row
        if (time.time() - cached_at) < (14 * 86400):  # 14 days validity
            return {"asin": asin.upper(), "status": status, "reason": reason, "cached": True}
    return None


def cache_gating_status(asin: str, brand: str, status: str, reason: str):
    """Caches gating status in SQLite."""
    conn = sqlite3.connect(CACHE_DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO gating_records (asin, brand, status, reason, cached_at)
        VALUES (?, ?, ?, ?, ?)
    """, (asin.upper(), (brand or "").upper(), status, reason, int(time.time())))
    conn.commit()
    conn.close()


def check_asin_gating(asin: str, brand: str = "") -> Dict[str, Any]:
    """
    Checks if an ASIN can be listed in New condition.
    Returns status: 'UNGATED', 'GATED_APPROVAL_REQUIRED', or 'ERROR_CHECK_MANUAL'.
    """
    clean_asin = (asin or "").strip().upper()
    if not clean_asin:
        return {"asin": "", "status": "ERROR", "reason": "Empty ASIN"}

    # 1. Check local cache
    cached = get_cached_gating_status(clean_asin)
    if cached:
        return cached

    # 2. Check token
    token = get_lwa_access_token()
    if not token or not SELLER_ID:
        return {
            "asin": clean_asin,
            "status": "PASSIVE_ASSUME_UNGATED",
            "reason": "SP-API credentials not configured; manual check link generated.",
            "cached": False
        }

    # 3. Call Listings Restrictions API
    url = "https://sellingpartnerapi-na.amazon.com/listings/2021-08-01/restrictions"
    params = {
        "asin": clean_asin,
        "sellerId": SELLER_ID,
        "marketplaceIds": MARKETPLACE_ID,
        "conditionType": "new_new"
    }
    headers = {
        "x-amz-access-token": token,
        "Accept": "application/json"
    }

    try:
        res = requests.get(url, params=params, headers=headers, timeout=12)
        if res.status_code == 200:
            data = res.json()
            restrictions = data.get("restrictions", [])
            
            if not restrictions:
                status = "UNGATED"
                reason = "Eligible to list in New condition."
            else:
                status = "GATED_APPROVAL_REQUIRED"
                reasons = []
                for r in restrictions:
                    for reason_obj in r.get("reasons", []):
                        reasons.append(reason_obj.get("message", "Approval Required"))
                reason = "; ".join(reasons) if reasons else "Restricted by brand or sub-category."

            cache_gating_status(clean_asin, brand, status, reason)
            return {"asin": clean_asin, "status": status, "reason": reason, "cached": False}

        elif res.status_code == 429:
            logger.warning(f"SP-API Rate Limited on ASIN {clean_asin}. Yielding passive pass.")
            return {"asin": clean_asin, "status": "PASSIVE_ASSUME_UNGATED", "reason": "Rate limited; check Seller Central."}
        else:
            logger.error(f"Gating API HTTP {res.status_code} for {clean_asin}: {res.text}")
            return {"asin": clean_asin, "status": "ERROR_CHECK_MANUAL", "reason": f"HTTP {res.status_code}"}

    except Exception as e:
        logger.error(f"Exception calling Listings Restrictions API: {e}")
        return {"asin": clean_asin, "status": "ERROR_CHECK_MANUAL", "reason": str(e)}


def calculate_balanced_deal_rating(base_score: int, gating_status: str) -> int:
    """
    Applies the +10 point bonus if the product is verified UNGATED.
    Caps score at 99.
    """
    score = int(base_score)
    if gating_status == "UNGATED":
        score += 10
    elif gating_status == "GATED_APPROVAL_REQUIRED":
        score -= 5
    return max(1, min(99, score))


if __name__ == "__main__":
    test_asin = "B0000302W6"  # Klein Tools benchmark
    result = check_asin_gating(test_asin, brand="Klein Tools")
    print(f"Gating Result for {test_asin}:")
    print(json.dumps(result, indent=2))
    print(f"Base Score 80 -> Adjusted Score: {calculate_balanced_deal_rating(80, result['status'])}")
